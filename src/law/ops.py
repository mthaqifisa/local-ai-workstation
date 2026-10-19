"""Operations: storage reclaim, uninstall, and secrets export/set."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from rich.console import Console

from law import registry, secrets
from law.install import launchd
from law.install.engine import owned
from law.paths import LawPaths
from law.sysapi import SysAPI


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def _truncate_tail(path: Path, keep_bytes: int) -> None:
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - keep_bytes))
        tail = fh.read()
    path.write_bytes(tail)


def reclaim(paths: LawPaths, sysapi: SysAPI, con: Console, *, trash_days: int = 7, log_days: int = 14,
            dry_run: bool = False) -> int:
    """Free disk: stale HF revisions/repos not in the registry, old trash and logs, tool caches."""
    reg = registry.load(paths.registry_file)
    freed = 0
    now = time.time()

    wanted = reg.hf_repos_in_use()
    hub = paths.models / "hf" / "hub"
    if hub.is_dir():
        for repo_dir in sorted(hub.glob("models--*")):
            repo = repo_dir.name.removeprefix("models--").replace("--", "/")
            if repo not in wanted:
                n = _size(repo_dir)
                con.print(f"remove unused model cache {repo} ({_gb(n)})")
                if not dry_run:
                    shutil.rmtree(repo_dir)
                freed += n

    # Language-model folders no longer in the registry (e.g. a quant that was upgraded).
    keep_llm = {m.id for m in reg.llm_models}
    if paths.llm_models.is_dir():
        for model_dir in sorted(paths.llm_models.iterdir()):
            if model_dir.is_dir() and model_dir.name not in keep_llm and (model_dir / ".law-complete").exists():
                n = _size(model_dir)
                con.print(f"remove retired model {model_dir.name} ({_gb(n)})")
                if not dry_run:
                    shutil.rmtree(model_dir)
                freed += n

    # Partial downloads orphaned by a killed download (older than an hour, so never an active one).
    if hub.is_dir():
        for part in hub.rglob("*.incomplete"):
            if now - part.stat().st_mtime > 3600:
                n = part.stat().st_size
                con.print(f"remove partial download {part.name[:16]}… ({_gb(n)})")
                if not dry_run:
                    part.unlink(missing_ok=True)
                freed += n

    for base, days in ((paths.trash, trash_days), (paths.logs, log_days)):
        if not base.is_dir():
            continue
        for item in base.iterdir():
            # Live service logs (*.log) are written continuously, so they never age out; they are
            # rotated by size instead. Everything else goes once it is older than the retention period.
            if item.suffix == ".log" and item.is_file():
                if item.stat().st_size > 200_000_000:
                    con.print(f"truncate {item} (keeping the last 10 MB)")
                    if not dry_run:
                        _truncate_tail(item, 10_000_000)
                continue
            if now - item.stat().st_mtime > days * 86400:
                n = _size(item)
                con.print(f"remove {item} ({_gb(n)})")
                if not dry_run:
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
                freed += n

    tmp = paths.tmp
    if tmp.is_dir():
        for item in tmp.iterdir():
            if now - item.stat().st_mtime > 86400:
                n = _size(item)
                if not dry_run:
                    shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
                freed += n

    if not dry_run:
        before = _size(paths.cache / "uv")
        sysapi.run(["uv", "cache", "prune"], env={"UV_CACHE_DIR": str(paths.cache / "uv")}, timeout=600)
        freed += max(0, before - _size(paths.cache / "uv"))
        sysapi.run(["brew", "cleanup", "-s"], timeout=1200)
        sock = paths.home / "colima" / "default" / "docker.sock"
        if sock.exists():
            sysapi.run(["docker", "image", "prune", "-f"], env={"DOCKER_HOST": f"unix://{sock}"}, timeout=600)
    con.print(f"[bold]{'would free' if dry_run else 'freed'} ~{_gb(freed)}[/] (plus Homebrew/Docker caches)")
    return freed


def uninstall(paths: LawPaths, sysapi: SysAPI, con: Console, *, system: bool = False) -> None:
    """Stop and remove LAW's services and containers. `system` also removes the ~/.zprofile block.

    ~/LAW itself (models, outputs, secrets) is left in place; delete it yourself for a full reset.
    """
    data = owned(paths)
    sock = paths.home / "colima" / "default" / "docker.sock"
    if sock.exists():
        con.print("stopping containers")
        sysapi.run(["docker-compose", "-f", str(paths.repo / "compose.yaml"), "down"],
                   env={"DOCKER_HOST": f"unix://{sock}", "LAW_HOME": str(paths.home), "LAW_REPO": str(paths.repo)},
                   timeout=600)
    for label in sorted(set(data.get("launchd", [])) | {p.stem for p in paths.launch_agents.glob("com.law6.*.plist")}):
        if launchd.remove(paths, label, sysapi):
            con.print(f"removed service {label}")
    if system:
        zp = paths.user_home / ".zprofile"
        if zp.exists():
            from law.install.steps import ZPROFILE_END, ZPROFILE_MARK
            text = zp.read_text()
            if ZPROFILE_MARK in text:
                start = text.index(ZPROFILE_MARK)
                end = text.index(ZPROFILE_END, start) + len(ZPROFILE_END) + 1
                zp.write_text(text[:start] + text[end:])
                con.print(f"removed LAW block from {zp}")
    (paths.state / "install.json").write_text(json.dumps({"uninstalled": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    con.print("LAW services removed. Data remains in ~/LAW (rm -rf ~/LAW for a full reset).")


def secrets_export(paths: LawPaths, dest: Path) -> None:
    values = secrets.read(paths.env_file)
    keep = {k: v for k, v in values.items() if k in secrets.USER_PROVIDED}
    secrets.write(dest, keep)


def secrets_set(paths: LawPaths, key: str, value: str) -> None:
    if key not in secrets.USER_PROVIDED:
        raise ValueError(f"{key} is not a user-settable secret ({', '.join(secrets.USER_PROVIDED)})")
    secrets.ensure(paths.env_file, imported={key: value})
