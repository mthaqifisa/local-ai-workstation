"""Small, dependency-free helpers: logging, subprocess, atomic writes, locking, .env."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

# ── logging ─────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def head(msg: str) -> None:
    print(_c("36", "==> ") + msg, flush=True)


def ok(msg: str) -> None:
    print("  " + _c("32", "✓ ") + msg, flush=True)


def skip(msg: str) -> None:
    print("  " + _c("90", "· " + msg), flush=True)


def warn(msg: str) -> None:
    print("  " + _c("33", "! ") + msg, file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(_c("31", "✗ ") + msg, file=sys.stderr, flush=True)


def is_macos() -> bool:
    return sys.platform == "darwin"


# ── subprocess ──────────────────────────────────────────────────────────
def run(cmd: Sequence[str], *, check: bool = True, capture: bool = True,
        timeout: float | None = None, env: dict | None = None,
        cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run a command (never via a shell). On failure (check=True) the error message
    includes the tail of stderr, so install-error.log shows the real cause."""
    r = subprocess.run(list(cmd), check=False, text=True,
                       capture_output=capture, timeout=timeout,
                       env={**os.environ, **(env or {})}, cwd=cwd)
    if check and r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip()[-800:] if capture else ""
        raise RuntimeError(f"`{' '.join(map(str, cmd))}` failed (exit {r.returncode})"
                           + (f": {tail}" if tail else ""))
    return r


def out(cmd: Sequence[str], timeout: float = 60) -> str:
    """Return stdout, or '' if the command is missing/fails."""
    try:
        return run(cmd, check=False, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def which(name: str) -> str | None:
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin")]
    path = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    return shutil.which(name, path=path)


# ── files ───────────────────────────────────────────────────────────────
def atomic_write(path: Path, data: str | bytes, mode: int = 0o644) -> bool:
    """Write via temp file + rename. Returns True if the content changed.
    Unchanged content is NOT rewritten (keeps mtimes stable → no spurious restarts)."""
    path = Path(path)
    raw = data.encode() if isinstance(data, str) else data
    if path.exists() and path.read_bytes() == raw:
        if (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def copy_if_absent(src: Path, dst: Path) -> bool:
    if dst.exists() or not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
    return True


def rotate_logs(folder: Path, max_mb: float = 20, keep: int = 2) -> list[str]:
    """Copy-truncate every *.log bigger than max_mb: x.log → x.log.1 (→ .2 …, `keep` copies).
    Copy-truncate (not rename) because launchd and the model process keep the file open in
    append mode — after the truncate they simply continue writing at the new end."""
    rotated: list[str] = []
    folder = Path(folder)
    if not folder.is_dir():
        return rotated
    for f in sorted(folder.glob("*.log")):
        try:
            if f.stat().st_size <= max_mb * 1024 * 1024:
                continue
            for i in range(keep - 1, 0, -1):
                older = f.with_name(f"{f.name}.{i}")
                if older.exists():
                    os.replace(older, f.with_name(f"{f.name}.{i + 1}"))
            if keep > 0:
                shutil.copyfile(f, f.with_name(f"{f.name}.1"))
            with open(f, "r+b") as fh:
                fh.truncate(0)
            rotated.append(f.name)
        except OSError:
            continue
    return rotated


# ── locking ─────────────────────────────────────────────────────────────
class LockBusy(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path, wait: float = 0) -> Iterator[None]:
    """Exclusive advisory lock. Released automatically if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                raise LockBusy(f"another law process holds {path}")
            time.sleep(0.5)
    try:
        fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ── .env (KEY=VALUE, chmod 600) ─────────────────────────────────────────
def read_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        data[k] = v
    return data


def write_env(path: Path, data: dict[str, str]) -> bool:
    body = "# Local AI Workstation v2 secrets — chmod 600, never commit\n"
    body += "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
    return atomic_write(path, body, mode=0o600)


def merge_env(path: Path, new: dict[str, str]) -> list[str]:
    """Add keys that are missing; never overwrite existing values. Returns added keys."""
    cur = read_env(path)
    added = [k for k, v in new.items() if v and not cur.get(k)]
    if added:
        cur.update({k: new[k] for k in added})
    write_env(path, cur)          # also enforces chmod 600
    return added
