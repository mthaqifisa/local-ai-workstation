"""Every call to a real macOS tool goes through this class.
Tests replace it with FakeSystem (tests/fakes.py), so the reconcile logic is tested
without Homebrew, Colima or Docker present."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .util import out, run, which


class SystemError_(RuntimeError):
    pass


def _need(tool: str) -> str:
    p = which(tool)
    if not p:
        raise SystemError_(f"'{tool}' not found on PATH")
    return p


class System:
    # ── Homebrew ─────────────────────────────────────────────────────────
    def brew_installed(self) -> set[str]:
        r = run([_need("brew"), "list", "--formula", "-1"], check=False, timeout=300)
        if r.returncode != 0:
            raise SystemError_("`brew list` failed: " + r.stderr.strip()[:300])
        return set(r.stdout.split())

    def brew_install(self, f: str) -> None:
        run([_need("brew"), "install", f], capture=False, timeout=7200)

    def brew_casks_installed(self) -> set[str]:
        r = run([_need("brew"), "list", "--cask", "-1"], check=False, timeout=300)
        return set(r.stdout.split()) if r.returncode == 0 else set()

    def brew_install_cask(self, c: str) -> None:
        run([_need("brew"), "install", "--cask", c], capture=False, timeout=7200)

    def brew_uninstall(self, f: str) -> None:
        run([_need("brew"), "uninstall", f], capture=False, timeout=1800)

    def brew_dependents(self, f: str) -> list[str]:
        return out([_need("brew"), "uses", "--installed", "--recursive", f], timeout=300).split()

    # ── Colima ───────────────────────────────────────────────────────────
    def colima_info(self) -> dict | None:
        """{'status': 'Running', 'cpus': 4, 'memory_gb': 4, 'disk_gb': 60} or None."""
        raw = out([_need("colima"), "list", "--json"], timeout=60)
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("name", "default") == "default":
                gb = lambda v: round(int(v or 0) / 1024**3)
                return {"status": d.get("status", ""), "cpus": int(d.get("cpus") or 0),
                        "memory_gb": gb(d.get("memory")), "disk_gb": gb(d.get("disk"))}
        return None

    def colima_start(self, cpu: int, mem: int, disk: int) -> None:
        run([_need("colima"), "start", "--cpu", str(cpu), "--memory", str(mem),
             "--disk", str(disk)], capture=False, timeout=1800)

    def colima_stop(self) -> None:
        run([_need("colima"), "stop"], capture=False, timeout=600)

    # ── Docker (inside Colima) ───────────────────────────────────────────
    def docker_ok(self) -> bool:
        return bool(which("docker")) and run(["docker", "info"], check=False, timeout=60).returncode == 0

    def docker_containers(self) -> list[dict]:
        """[{'name', 'state', 'image', 'labels': {..}}] for ALL containers."""
        r = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], check=False, timeout=60)
        if r.returncode != 0:
            raise SystemError_("`docker ps` failed: " + r.stderr.strip()[:300])
        res = []
        for line in r.stdout.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            labels = {}
            for kv in (d.get("Labels") or "").split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    labels[k] = v
            res.append({"name": d.get("Names", ""), "state": d.get("State", ""),
                        "image": d.get("Image", ""), "labels": labels})
        return res

    def docker_rm(self, name: str) -> None:
        run(["docker", "rm", "-f", name], timeout=300)

    def docker_start(self, name: str) -> None:
        run(["docker", "start", name], timeout=300)

    def docker_pull(self, image: str) -> None:
        run(["docker", "pull", image], capture=False, timeout=3600)

    def docker_run(self, args: list[str]) -> None:
        run(["docker", "run", "-d"] + args, timeout=600)

    # ── Python envs (uv) ─────────────────────────────────────────────────
    def venv_python_ok(self, venv: Path, version: str) -> bool:
        py = venv / "bin" / "python"
        if not py.exists():
            return False
        r = run([str(py), "-c", "import sys;print('%d.%d'%sys.version_info[:2])"], check=False, timeout=60)
        return r.returncode == 0 and r.stdout.strip() == version

    def venv_create(self, venv: Path, version: str) -> None:
        shutil.rmtree(venv, ignore_errors=True)
        run([_need("uv"), "venv", "--python", version, str(venv)], timeout=900)

    def uv_compile(self, req_in: Path, lock: Path, python: Path) -> None:
        run([_need("uv"), "pip", "compile", str(req_in), "-o", str(lock),
             "--python", str(python), "--quiet"], timeout=1800)

    def uv_sync(self, lock: Path, python: Path) -> None:
        run([_need("uv"), "pip", "sync", str(lock), "--python", str(python)],
            capture=False, timeout=7200)

    def uv_freeze(self, python: Path) -> str:
        return out([_need("uv"), "pip", "freeze", "--python", str(python)], timeout=300)

    # ── Hugging Face ─────────────────────────────────────────────────────
    def hf_download(self, python: Path, repo: str, token: str | None) -> None:
        code = ("import sys\nfrom huggingface_hub import snapshot_download\n"
                "snapshot_download(sys.argv[1])\n")
        env = {"HF_HUB_ENABLE_HF_TRANSFER": "0"}
        if token:
            env["HF_TOKEN"] = token
        r = subprocess.run([str(python), "-c", code, repo], env={**os.environ, **env})
        if r.returncode != 0:
            raise SystemError_(f"download of {repo} failed (exit {r.returncode}). "
                               "Re-run to resume; gated repos need HF_TOKEN in ~/.law/.env")

    # ── misc ─────────────────────────────────────────────────────────────
    def free_gb(self, path: Path) -> float:
        p = Path(path)
        while not p.exists():
            p = p.parent
        return shutil.disk_usage(p).free / 1024**3

    def launchctl_bootout(self, label: str) -> None:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], check=False, timeout=60)

    # ── launchd services ─────────────────────────────────────────────────
    def launchd_loaded(self, label: str) -> bool:
        return run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=False, timeout=30).returncode == 0

    def launchd_load(self, label: str, plist: Path) -> None:
        """(Re)load: bootout if loaded, then bootstrap. Retries while launchd finishes the bootout."""
        dom = f"gui/{os.getuid()}"
        if self.launchd_loaded(label):
            run(["launchctl", "bootout", f"{dom}/{label}"], check=False, timeout=90)
        last = None
        for _ in range(10):
            r = run(["launchctl", "bootstrap", dom, str(plist)], check=False, timeout=60)
            if r.returncode == 0 or self.launchd_loaded(label):
                return
            last = (r.stderr or r.stdout).strip()
            time.sleep(1)
        raise SystemError_(f"launchctl bootstrap {label} failed: {last}")

    def launchd_unload(self, label: str) -> None:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], check=False, timeout=90)

    def launchd_kickstart(self, label: str) -> None:
        run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], check=False, timeout=90)

    def http_ok(self, url: str, wait: float = 20) -> bool:
        deadline = time.time() + wait
        while True:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    if r.status == 200:
                        return True
            except Exception:  # noqa: BLE001
                pass
            if time.time() >= deadline:
                return False
            time.sleep(1)
