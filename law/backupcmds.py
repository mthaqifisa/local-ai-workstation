"""`law backup` / `law restore` — your settings and memory in one file.

A backup holds what can't be re-downloaded: ~/.law/config (your models + personas),
~/.law/.env (secrets — leave out with --no-secrets), and the state that carries your work:
the agent store (specs, memory facts), RAG index, Telegram chats + schedules, probe results and
the job history. Optionally (--webui) the Open WebUI volume (your chats and accounts there).
Models, the runtime venv and logs are NOT included — `./install.sh` rebuilds them.

Saved to ~/.law/backups/law-<time>.tar.gz (chmod 600). Restore asks before overwriting and
stops the services while it copies.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .paths import get_paths
from .util import err, ok, warn

STATE_FILES = ["agent.db", "rag.db", "queue.db", "telegram.json", "probes.json", "owned.json"]


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite db (WAL-safe) via the backup API."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def create(dest: Path | None = None, *, secrets: bool = True, webui: bool = False) -> Path:
    p = get_paths()
    p.backups.mkdir(parents=True, exist_ok=True)
    dest = dest or p.backups / f"law-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "law-backup"
        (stage / "state").mkdir(parents=True)
        for name in STATE_FILES:
            f = p.state / name
            if not f.exists():
                continue
            if f.suffix == ".db":
                _sqlite_copy(f, stage / "state" / name)
            else:
                (stage / "state" / name).write_bytes(f.read_bytes())
        if webui:
            vol = stage / "open-webui.tar"
            r = subprocess.run(["docker", "run", "--rm", "-v", "open-webui:/data:ro", "-v", f"{stage}:/out",
                                "alpine", "tar", "-cf", "/out/open-webui.tar", "-C", "/data", "."],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0 or not vol.exists():
                warn(f"Open WebUI volume not included: {(r.stderr or r.stdout).strip()[:200]}")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            if p.config.is_dir():
                tar.add(p.config, arcname="config")
            if secrets and p.env_file.exists():
                tar.add(p.env_file, arcname=".env")
            tar.add(stage / "state", arcname="state")
            if (stage / "open-webui.tar").exists():
                tar.add(stage / "open-webui.tar", arcname="open-webui.tar")
    return dest


def restore(archive: Path) -> list[str]:
    """Extract a backup over ~/.law. Returns the restored top-level entries."""
    p = get_paths()
    done: list[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile() or m.isdir()]
        for m in members:                                   # no absolute paths / .. / links
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ValueError(f"unsafe path in backup: {m.name}")
        webui = next((m for m in members if m.name == "open-webui.tar"), None)
        for m in members:                                   # a stale WAL next to a replaced db corrupts it
            if m.name.startswith("state/") and m.name.endswith(".db"):
                for ext in ("-wal", "-shm"):
                    (p.law / (m.name + ext)).unlink(missing_ok=True)
        tar.extractall(p.law, members=[m for m in members if m is not webui], filter="data")
        done = sorted({m.name.split("/")[0] for m in members if m is not webui})
        if webui:
            with tempfile.TemporaryDirectory() as tmp:
                tar.extract(webui, tmp, filter="data")
                r = subprocess.run(["docker", "run", "--rm", "-v", "open-webui:/data", "-v", f"{tmp}:/in", "alpine",
                                    "sh", "-c", "tar -xf /in/open-webui.tar -C /data"],
                                   capture_output=True, text=True, timeout=1800)
                if r.returncode == 0:
                    done.append("open-webui volume")
                else:
                    warn(f"Open WebUI volume not restored: {(r.stderr or r.stdout).strip()[:200]}")
    if p.env_file.exists():
        os.chmod(p.env_file, 0o600)
    return done


def cmd_backup(a) -> int:
    dest = create(Path(a.out).expanduser() if a.out else None, secrets=not a.no_secrets, webui=a.webui)
    ok(f"backup saved: {dest} ({dest.stat().st_size / 1024**2:.1f} MB)"
       + ("" if not a.no_secrets else " — secrets NOT included"))
    return 0


def cmd_restore(a) -> int:
    archive = Path(a.archive).expanduser()
    if not archive.is_file():
        err(f"not a file: {archive}"); return 2
    if not a.yes:
        ans = input(f"Restore {archive.name} over ~/.law (config, secrets, agent memory, history)? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            warn("nothing restored"); return 1
    from .runcmds import cmd_start, cmd_stop
    cmd_stop(a)
    try:
        done = restore(archive)
    except (tarfile.TarError, ValueError, OSError) as e:
        err(f"restore failed: {e}"); return 1
    finally:
        cmd_start(a)
    ok("restored: " + ", ".join(done) + " — run ./install.sh if your models/config changed")
    return 0


def add_parsers(sub) -> None:
    s = sub.add_parser("backup", help="save settings, secrets, agent memory and history to one file")
    s.add_argument("-o", "--out", help="file to write (default ~/.law/backups/law-<time>.tar.gz)")
    s.add_argument("--no-secrets", action="store_true", help="leave ~/.law/.env (tokens) out")
    s.add_argument("--webui", action="store_true", help="also include the Open WebUI volume (needs Docker)")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore", help="restore a `law backup` file (stops services while copying)")
    s.add_argument("archive")
    s.add_argument("--yes", action="store_true", help="don't ask")
    s.set_defaults(fn=cmd_restore)
