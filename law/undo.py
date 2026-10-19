"""Undo for project folders: `law snapshot`, `law snapshots`, `law rollback`.

Each project gets a PRIVATE git history under ~/.law/state/undo/ that tracks the folder's
files (kept out of the project so nothing is added to it). It never touches your own .git
(if the project has one): different git dir, your .gitignore is respected, and .local-agent/
is never tracked. An older in-project history (<project>/.local-agent/undo.git) is moved
into the central location automatically the first time, so snapshots are preserved.

  snapshot  → commit every change since the last snapshot (no-op if nothing changed)
  rollback  → restore the files exactly as they were in a snapshot. A safety snapshot is
              taken first, so a rollback can itself be rolled back.
              Files created after the snapshot are removed; files deleted are restored.
              Ignored files (.gitignore) and files bigger than max_file_mb are left alone.

Agents (Phase 4) take a snapshot before every run. `--system` additionally asks macOS for
an APFS local snapshot (Time Machine) as a whole-disk backstop — restoring that one is done
from Time Machine / Recovery, not by `law rollback`.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

EXCLUDES = [".local-agent/", ".git/", ".DS_Store", ".screenshots/"]   # .screenshots = agents' scratch
DEFAULT_MAX_FILE_MB = 50


class UndoError(RuntimeError):
    pass


@dataclass
class Snap:
    id: str
    ts: float
    message: str


def _central_gitdir(project: Path) -> Path:
    """A stable, collision-free git dir for this project under ~/.law/state/undo/."""
    from .paths import get_paths
    h = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    return get_paths().undo_dir / f"{project.name}-{h}.git"


class Undo:
    def __init__(self, project: Path, max_file_mb: float = DEFAULT_MAX_FILE_MB, gitdir: Path | None = None):
        self.project = Path(project).expanduser().resolve()
        self.gitdir = Path(gitdir) if gitdir else _central_gitdir(self.project)
        self.max_bytes = int(max_file_mb * 1024 * 1024)
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        """Move a pre-existing in-project history into the central location, once."""
        legacy = self.project / ".local-agent" / "undo.git"
        if legacy.is_dir() and not self.gitdir.exists():
            try:
                self.gitdir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(self.gitdir))
            except OSError:
                pass

    # ── plumbing ────────────────────────────────────────────────────────
    def _git(self, *args: str, check: bool = True) -> str:
        env = {**os.environ, "GIT_DIR": str(self.gitdir), "GIT_WORK_TREE": str(self.project),
               "GIT_AUTHOR_NAME": "law", "GIT_AUTHOR_EMAIL": "law@localhost",
               "GIT_COMMITTER_NAME": "law", "GIT_COMMITTER_EMAIL": "law@localhost",
               "GIT_TERMINAL_PROMPT": "0"}
        for k in ("GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_CEILING_DIRECTORIES"):
            env.pop(k, None)
        r = subprocess.run(["git", *args], cwd=self.project, env=env, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise UndoError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:400]}")
        return r.stdout

    def ready(self) -> bool:
        return (self.gitdir / "HEAD").exists()

    def init(self) -> None:
        if not self.project.is_dir():
            raise UndoError(f"not a folder: {self.project}")
        if self.ready():
            return
        self.gitdir.parent.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q")
        self._git("config", "core.autocrlf", "false")
        self._git("config", "core.quotepath", "false")
        self._git("config", "gc.auto", "256")
        (self.gitdir / "info").mkdir(exist_ok=True)
        (self.gitdir / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n")

    def _exclude_big_staged(self) -> list[str]:
        big = []
        for rel in self._git("diff", "--cached", "--name-only", "--diff-filter=AM", "-z").split("\0"):
            if not rel:
                continue
            f = self.project / rel
            try:
                if f.is_file() and f.stat().st_size > self.max_bytes:
                    big.append(rel)
            except OSError:
                pass
        if big:
            self._git("rm", "-q", "--cached", "--", *big)
            with (self.gitdir / "info" / "exclude").open("a") as fh:
                fh.write("".join(f"/{rel}\n" for rel in big))
        return big

    def head(self) -> str | None:
        out = self._git("rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        return out[:12] if out else None

    # ── public ──────────────────────────────────────────────────────────
    def _ensure_excludes(self) -> None:
        """Histories made by an older law lack newer EXCLUDES entries — add them (keeps big-file lines)."""
        f = self.gitdir / "info" / "exclude"
        have = f.read_text().splitlines() if f.exists() else []
        missing = [e for e in EXCLUDES if e not in have]
        if missing:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("\n".join(have + missing) + "\n")

    def snapshot(self, message: str = "snapshot") -> tuple[str, int, list[str]]:
        """→ (snapshot id, files changed, big files skipped). 0 changed = nothing new."""
        self.init()
        self._ensure_excludes()
        self._git("add", "-A")
        skipped = self._exclude_big_staged()
        has_head = self.head() is not None
        changed = [x for x in self._git("diff", "--cached", "--name-only", "-z").split("\0") if x] \
            if has_head else [x for x in self._git("ls-files", "-z").split("\0") if x]
        if has_head and not changed:
            return self.head(), 0, skipped
        self._git("commit", "-q", "--allow-empty", "--no-verify", "-m", message)
        return self.head(), len(changed), skipped

    def list(self, limit: int = 30) -> list[Snap]:
        if not self.ready() or self.head() is None:
            return []
        out = self._git("log", f"-n{limit}", "--format=%H%x1f%ct%x1f%s")
        snaps = []
        for line in out.splitlines():
            sid, ts, msg = (line.split("\x1f") + ["", ""])[:3]
            snaps.append(Snap(sid[:12], float(ts or 0), msg))
        return snaps

    def changes_since(self, ref: str) -> list[str]:
        self._git("add", "-A")
        self._exclude_big_staged()
        return [x for x in self._git("diff", "--cached", "--name-only", "-z", ref).split("\0") if x]

    def rollback(self, ref: str | None = None) -> dict:
        """Restore files to snapshot `ref` (default: the latest snapshot)."""
        if not self.ready() or self.head() is None:
            raise UndoError(f"no snapshots yet for {self.project} — take one with `law snapshot`")
        target = ref or self.head()
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}", target or ""):
            raise UndoError(f"'{target}' is not a snapshot id (see `law snapshots`)")
        full = self._git("rev-parse", "--verify", "-q", f"{target}^{{commit}}", check=False).strip()
        if not full:
            raise UndoError(f"no snapshot {target} in {self.project} (see `law snapshots`)")
        changes = self.changes_since(full)
        if not changes:
            return {"target": target, "changed": [], "safety": None}
        safety, _, _ = self.snapshot(f"before rollback to {target} ({time.strftime('%Y-%m-%d %H:%M')})")
        self._git("read-tree", "-u", "--reset", full)
        self._git("commit", "-q", "--allow-empty", "--no-verify", "-m", f"rolled back to {target}")
        return {"target": target, "changed": changes, "safety": safety}


# ── macOS APFS local snapshot (whole-disk backstop) ─────────────────────
def system_snapshot() -> str:
    r = subprocess.run(["tmutil", "localsnapshot"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise UndoError("tmutil localsnapshot failed: " + (r.stderr or r.stdout).strip()[:300]
                        + " — Time Machine local snapshots may need Time Machine to be set up.")
    m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{6})", r.stdout)
    return m.group(1) if m else r.stdout.strip()
