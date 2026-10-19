"""`law doctor`: check services, models, ports and macOS permissions; print the LAN URLs."""

from __future__ import annotations

import socket
import subprocess

import httpx
from rich.console import Console
from rich.table import Table

from law import registry, secrets
from law.paths import LawPaths


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _port_up(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def _automation_ok() -> bool:
    """System Events answers only when Automation is allowed for the calling app."""
    proc = subprocess.run(["osascript", "-e", 'tell application "System Events" to count processes'],
                          capture_output=True, text=True, timeout=20, check=False)
    return proc.returncode == 0


def _screen_ok(paths: LawPaths) -> bool:
    """Without Screen Recording permission, screencapture writes only the wallpaper; a failure means no access."""
    probe = paths.tmp / "doctor-screen.png"
    probe.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["screencapture", "-x", str(probe)], capture_output=True, timeout=20, check=False)
    ok = proc.returncode == 0 and probe.exists() and probe.stat().st_size > 10_000
    probe.unlink(missing_ok=True)
    return ok


def run_doctor(paths: LawPaths, con: Console) -> bool:
    reg = registry.load(paths.registry_file)
    ok = True
    table = Table("check", "result", "detail")

    checks = [("valkey", reg.ports.valkey), ("oMLX (internal)", reg.ports.llm_internal),
              ("LLM endpoint", reg.ports.llm_public), ("job API", reg.ports.law_api),
              ("Open WebUI", reg.ports.open_webui), ("SearXNG", reg.ports.searxng),
              ("Homepage", reg.ports.homepage), ("ComfyUI", reg.ports.comfyui)]
    for name, port in checks:
        up = _port_up(port)
        ok = ok and (up or name in {"ComfyUI", "Homepage"})
        table.add_row(name, "[green]up[/]" if up else "[red]down[/]", f":{port}")

    present = sum((paths.llm_models / m.id / ".law-complete").exists() for m in reg.llm_models)
    table.add_row("models", f"{present}/{len(reg.llm_models)}",
                  "[green]all present[/]" if present == len(reg.llm_models) else "[yellow]missing[/]")
    if present != len(reg.llm_models):
        ok = False

    for name, ok_now in (("Mac control (Automation)", _automation_ok()), ("Screen capture", _screen_ok(paths))):
        table.add_row(name, "[green]granted[/]" if ok_now else "[yellow]not granted[/]",
                      "" if ok_now else "System Settings → Privacy & Security")

    values = secrets.read(paths.env_file)
    tg = "configured" if values.get("LAW_TELEGRAM_TOKEN") else "not set"
    ids = secrets.allowed_telegram_ids(values)
    table.add_row("Telegram", tg, f"{len(ids)} allowed id(s)" if ids else "")

    try:
        h = httpx.get(f"http://127.0.0.1:{reg.ports.law_api}/healthz", timeout=5).json()
        table.add_row("queue", "[green]ok[/]", f"queued={h.get('queued')} running={h.get('running')}")
    except (httpx.HTTPError, OSError):
        table.add_row("queue", "[red]unreachable[/]", "")
        ok = False

    con.print(table)
    ip = _lan_ip()
    con.print("\n[bold]LAN access (no login):[/]")
    con.print(f"  LAW Console       : http://{ip}:{reg.ports.law_api}")
    con.print(f"  Chat / Open WebUI : http://{ip}:{reg.ports.open_webui}")
    con.print(f"  LLM API           : http://{ip}:{reg.ports.llm_public}/v1")
    con.print(f"  Dashboard         : http://{ip}:{reg.ports.homepage}")
    con.print(f"  law CLI elsewhere : export LAW_SERVER=http://{ip}:{reg.ports.law_api}")
    con.print(f"\n{'[green]all healthy[/]' if ok else '[yellow]some checks failed[/]'}")
    return ok
