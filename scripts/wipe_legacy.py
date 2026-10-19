#!/usr/bin/env python3
"""Remove every v4/v5 (and older) local-AI artifact listed in settings/legacy_inventory.toml.

Dry-run by default: prints what would happen and the size of each item.
`--apply` performs the wipe: archive -> stop services -> delete -> clean shell rc -> brew -> report.

Only the Python standard library is used so this runs on a fresh Mac before anything is installed.
External commands go through `SysOps` so the test-suite can replace them with fakes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = REPO / "settings" / "legacy_inventory.toml"


class SysOps:
    """Thin wrapper over the external commands the wipe needs."""

    def run(self, cmd: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def uid(self) -> int:
        return os.getuid()

    def which(self, name: str) -> str | None:
        return shutil.which(name)


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    freed_bytes: int = 0

    def log(self, msg: str) -> None:
        self.actions.append(msg)
        print(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"ERROR: {msg}", file=sys.stderr)


def size_of(path: Path) -> int:
    """Total bytes under `path` without following symlinks; 0 if missing."""
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    if not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


class Wiper:
    def __init__(self, home: Path, law_root: Path, inventory: dict, ops: SysOps, tmpdir: Path,
                 apply: bool, run_id: str) -> None:
        self.home = home
        self.law = law_root
        self.inv = inventory
        self.ops = ops
        self.tmpdir = tmpdir
        self.apply = apply
        self.archive_dir = law_root / "archive" / f"legacy-{run_id}"
        self.report = Report()

    # ---- helpers -------------------------------------------------------
    def _items(self, category: str) -> list[Path]:
        out: list[Path] = []
        for block in self.inv.get("paths", []):
            if block["category"] == category:
                out += [self.home / p for p in block["items"]]
        return out

    def _keep(self) -> set[Path]:
        return {p.resolve() if p.exists() else p for p in self._items("keep")}

    def _guard(self, path: Path) -> bool:
        """Refuse anything outside $HOME, $HOME itself, or a keep-listed path."""
        path = Path(os.path.abspath(path))  # collapse ".." before the containment check
        try:
            path.relative_to(self.home)
        except ValueError:
            return False
        return path != self.home and path not in self._keep()

    # ---- steps ---------------------------------------------------------
    def archive(self) -> None:
        targets = [p for p in self._items("archive") if p.exists() or p.is_symlink()]
        if not targets:
            return
        tar_path = self.archive_dir / "legacy.tar.gz"
        self.report.log(f"[archive] {len(targets)} items -> {tar_path}")
        if not self.apply:
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.archive_dir, 0o700)
        with tarfile.open(tar_path, "w:gz") as tar:
            for p in targets:
                tar.add(p, arcname=str(p.relative_to(self.home)),
                        filter=lambda ti: None if "/.venv/" in f"/{ti.name}/" or "__pycache__" in ti.name else ti)
        os.chmod(tar_path, 0o600)
        with tarfile.open(tar_path, "r:gz") as tar:  # integrity check before anything is deleted
            names = tar.getnames()
        missing = [p for p in targets if str(p.relative_to(self.home)) not in names]
        if missing:
            raise RuntimeError(f"archive verification failed for: {missing}")
        self.report.log(f"[archive] verified {len(names)} entries")

    def stop_services(self) -> None:
        agents_dir = self.home / "Library" / "LaunchAgents"
        uid = self.ops.uid()
        for block in self.inv.get("launch_agents", []):
            for label in block["labels"]:
                plist = agents_dir / f"{label}.plist"
                if not plist.exists():
                    continue
                self.report.log(f"[launchd] bootout + remove {label}")
                if self.apply:
                    self.ops.run(["launchctl", "bootout", f"gui/{uid}/{label}"])
                    plist.unlink()
        if self.ops.which("colima"):
            for profile in self.inv.get("docker", {}).get("colima_profiles", []):
                if (self.home / ".colima" / profile).exists():
                    self.report.log(f"[colima] stop + delete profile {profile}")
                    if self.apply:
                        self.ops.run(["colima", "stop", "--force", "--profile", profile])
                        code, out = self.ops.run(["colima", "delete", "--force", "--profile", profile])
                        if code != 0:
                            self.report.error(f"colima delete {profile}: {out}")

    def delete_paths(self) -> None:
        for p in self._items("delete"):
            if not (p.exists() or p.is_symlink()):
                continue
            if not self._guard(p):
                self.report.error(f"refusing to delete guarded path {p}")
                continue
            n = size_of(p)
            self.report.log(f"[delete] {p} ({human(n)})")
            if self.apply:
                remove(p)
                self.report.freed_bytes += n

    def delete_globs(self) -> None:
        bin_dir = self.home / ".local" / "bin"
        for base, patterns in ((bin_dir, self.inv.get("bin_shims", {}).get("patterns", [])),
                               (self.tmpdir, self.inv.get("tmp", {}).get("patterns", []))):
            if not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                if entry.name == "claude":
                    continue
                if any(fnmatch.fnmatch(entry.name, pat) for pat in patterns):
                    n = size_of(entry)
                    self.report.log(f"[delete] {entry} ({human(n)})")
                    if self.apply:
                        remove(entry)
                        self.report.freed_bytes += n

    def clean_shell_rc(self) -> None:
        cfg = self.inv.get("shell_rc", {})
        for name in cfg.get("files", []):
            rc = self.home / name
            if not rc.is_file():
                continue
            lines = rc.read_text().splitlines(keepends=True)
            # Case-insensitive so comment headers such as "# OpenClaw Completion" go with their block.
            pats = [pat.lower() for pat in cfg.get("patterns", [])]
            kept = [ln for ln in lines if not any(pat in ln.lower() for pat in pats)]
            removed = len(lines) - len(kept)
            if removed:
                self.report.log(f"[shell] {rc}: remove {removed} line(s)")
                if self.apply:
                    shutil.copy2(rc, rc.with_name(rc.name + ".bak.law6"))
                    rc.write_text("".join(kept))

    def brew(self) -> None:
        if not self.ops.which("brew"):
            return
        code, installed = self.ops.run(["brew", "list", "--formula", "-1"])
        if code != 0:
            return
        installed_set = set(installed.split())
        for formula in self.inv.get("brew", {}).get("formulae", []):
            if formula not in installed_set:
                continue
            _, users = self.ops.run(["brew", "uses", "--installed", formula])
            remaining = [u for u in users.split() if u not in self.inv["brew"]["formulae"]]
            if remaining:
                self.report.log(f"[brew] keep {formula} (needed by {', '.join(remaining)})")
                continue
            self.report.log(f"[brew] uninstall {formula}")
            if self.apply:
                code, out = self.ops.run(["brew", "uninstall", "--ignore-dependencies", formula])
                # Removing an earlier formula can take this one with it; that is success.
                if code != 0 and "No such keg" not in out:
                    self.report.error(f"brew uninstall {formula}: {out}")

    def run(self) -> Report:
        mode = "APPLY" if self.apply else "DRY-RUN"
        self.report.log(f"== wipe_legacy {mode} (home={self.home}) ==")
        self.archive()
        self.stop_services()
        self.delete_paths()
        self.delete_globs()
        self.clean_shell_rc()
        self.brew()
        self.report.log(f"== freed {human(self.report.freed_bytes)}; errors: {len(self.report.errors)} ==")
        return self.report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="perform the wipe (default is a dry run)")
    ap.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--law-root", type=Path, default=None)
    ap.add_argument("--report", type=Path, default=None, help="write a JSON report here")
    args = ap.parse_args(argv)

    home = args.home.resolve()
    law_root = (args.law_root or home / "LAW").resolve()
    inventory = tomllib.loads(args.inventory.read_text())
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))  # noqa: S108 - only to find stale legacy files
    wiper = Wiper(home, law_root, inventory, SysOps(), tmpdir, args.apply, run_id)
    report = wiper.run()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"apply": args.apply, "freed_bytes": report.freed_bytes,
                                           "actions": report.actions, "errors": report.errors}, indent=2))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
