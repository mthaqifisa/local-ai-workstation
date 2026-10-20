"""LAW Console: host-level stats and the web page (served by the native API, so numbers are the Mac's,
not the Docker VM's)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import psutil
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from law import client_setup, registry
from law import console_edit as edit
from law.paths import LawPaths

STATIC = Path(__file__).parent / "static"
DATA_VOLUME = "/System/Volumes/Data"  # where user data lives on modern macOS ("/" is the read-only system volume)


def _gpu_percent() -> int | None:
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'"Device Utilization %"=(\d+)', out)
    return int(m.group(1)) if m else None


def _memory() -> dict[str, float]:
    """Memory as Activity Monitor reports it: used = app + wired + compressed; cached files are reclaimable."""
    vm = psutil.virtual_memory()
    gb = 1024 ** 3
    wired = getattr(vm, "wired", 0)
    used = vm.total - vm.available
    return {"total_gb": round(vm.total / gb, 1), "used_gb": round(used / gb, 1),
            "wired_gb": round(wired / gb, 1), "percent": round(100 * used / vm.total, 1),
            "swap_gb": round(psutil.swap_memory().used / gb, 1)}


def system_stats(llm_internal: str) -> dict[str, Any]:
    disk = shutil.disk_usage(DATA_VOLUME if Path(DATA_VOLUME).exists() else "/")
    gb = 1024 ** 3
    loaded: list[str] = []
    try:
        status = httpx.get(f"{llm_internal}/v1/models/status", timeout=3).json()
        loaded = [m["id"] for m in status.get("models", []) if m.get("loaded")]
    except (httpx.HTTPError, ValueError):
        pass
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_cores": psutil.cpu_count(),
        "memory": _memory(),
        "disk": {"total_gb": round(disk.total / gb), "used_gb": round(disk.used / gb),
                 "free_gb": round(disk.free / gb), "percent": round(100 * disk.used / disk.total, 1)},
        "gpu_percent": _gpu_percent(),
        "loaded_models": loaded,
    }


def mount(app: Any, llm_internal: str, paths: LawPaths) -> None:
    router = APIRouter()

    @router.get("/system")
    def system() -> dict[str, Any]:
        return system_stats(llm_internal)

    @router.get("/roles")
    def get_roles() -> dict[str, Any]:
        return edit.list_roles(paths)

    @router.put("/roles/{name}")
    def put_role(name: str, upd: edit.RoleUpdate, tasks: BackgroundTasks) -> dict[str, str]:
        try:
            edit.update_role(paths, name, upd)
        except KeyError as exc:
            raise HTTPException(404, f"no role {name!r}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        tasks.add_task(edit.apply_async, paths, "opencode-config,services")
        return {"saved": name}

    @router.get("/settings")
    def get_settings() -> dict[str, Any]:
        return {"telegram": edit.telegram_status(paths), "email": edit.email_status(paths)}

    @router.put("/settings/telegram")
    def put_telegram(upd: edit.TelegramUpdate, tasks: BackgroundTasks) -> dict[str, str]:
        try:
            bot = edit.update_telegram(paths, upd)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        tasks.add_task(edit.apply_async, paths, "secrets,services")
        return {"bot": bot}

    @router.put("/settings/email")
    def put_email(upd: edit.EmailUpdate) -> dict[str, str]:
        """Read at send time, so no restart is needed."""
        try:
            return {"address": edit.update_email(paths, upd)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/client/opencode.json", response_class=PlainTextResponse)
    def client_opencode(request: Request) -> str:
        host = request.url.hostname or "127.0.0.1"
        return client_setup.as_json(client_setup.opencode_config(paths, host))

    @router.get("/client/install.sh", response_class=PlainTextResponse)
    def client_install(request: Request) -> str:
        reg = registry.load(paths.registry_file)
        return client_setup.install_script(request.url.hostname or "127.0.0.1", reg.ports.law_api,
                                           reg.ports.llm_public)

    @router.get("/", response_class=HTMLResponse)
    @router.get("/console", response_class=HTMLResponse)
    def console() -> str:
        return (STATIC / "console.html").read_text()

    app.include_router(router)
