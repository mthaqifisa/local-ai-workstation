"""The `law` command: install/doctor/status, service control, and AI tasks (local or over the LAN).

AI commands submit a job to the LAW API and stream progress. With LAW_SERVER set they run against
another Mac, so every device gets the same `law` commands.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from law import __version__
from law.paths import LawPaths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="LAW v6 — local AI workstation")
svc = typer.Typer(no_args_is_help=True, help="start/stop/restart/logs for services")
models_cli = typer.Typer(no_args_is_help=True, help="list/pull/verify models")
app.add_typer(svc, name="services")
app.add_typer(models_cli, name="models")
con = Console()
err = Console(stderr=True)


def _paths() -> LawPaths:
    return LawPaths.from_env()


def _server() -> str:
    env = os.environ.get("LAW_SERVER")
    if env:
        return env.rstrip("/")
    from law import registry
    reg = registry.load(_paths().registry_file)
    return f"http://127.0.0.1:{reg.ports.law_api}"


# ---- AI jobs --------------------------------------------------------------------------------
def _submit_and_wait(kind: str, payload: dict[str, Any], *, quiet: bool) -> dict[str, Any]:
    from law import client

    base = _server()
    try:
        job = client.submit(base, kind, payload, source="cli")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not quiet:
        err.print(f"[dim]job {job['id']} ({kind})[/]")

    def show(event: dict[str, Any]) -> None:
        name, data = event.get("event"), event.get("data", {})
        if quiet:
            return
        if name == "progress" and data.get("message"):
            err.print(f"[dim]  … {data['message']}[/]")
        elif name in {"failed", "dead"}:
            err.print(f"[red]  {name}: {data.get('error', '')}[/]")

    return client.wait(base, job["id"], show)


def _run_job(kind: str, payload: dict[str, Any], *, quiet: bool, field: str | None = None) -> None:
    job = _submit_and_wait(kind, payload, quiet=quiet)
    status = job.get("status")
    result = job.get("result") or {}
    if status != "done":
        err.print(f"[red]{status}: {job.get('error', 'no result')}[/]")
        raise typer.Exit(1)
    if field and field in result:
        con.print(result[field])
    elif "answer" in result:
        con.print(result["answer"])
    else:
        con.print_json(data=result)


@app.command()
def ask(prompt: Annotated[list[str], typer.Argument()], role: str = "assistant",
        quick: Annotated[bool, typer.Option("--quick", help="skip the review/revise loop")] = False,
        quiet: bool = False) -> None:
    """Ask a question (quality loop by default: draft, independent review, revise)."""
    _run_job("ask", {"prompt": " ".join(prompt), "role": role, "quality": not quick}, quiet=quiet)


@app.command()
def agent(role: str, task: Annotated[list[str], typer.Argument()],
          directory: Annotated[str | None, typer.Option("--dir")] = None, quiet: bool = False) -> None:
    """Run a role as a coding/operating agent (OpenCode) in a directory."""
    payload: dict[str, Any] = {"prompt": " ".join(task), "role": role}
    if directory:
        payload["dir"] = directory
    _run_job("task", payload, quiet=quiet)


@app.command()
def code(task: Annotated[list[str], typer.Argument()],
         directory: Annotated[str | None, typer.Option("--dir")] = None, quiet: bool = False) -> None:
    """Shortcut for `law agent coder`."""
    payload: dict[str, Any] = {"prompt": " ".join(task), "role": "coder"}
    if directory:
        payload["dir"] = directory
    _run_job("task", payload, quiet=quiet)


@app.command()
def image(prompt: Annotated[list[str], typer.Argument()], quiet: bool = False) -> None:
    """Generate an image."""
    _run_job("image", {"prompt": " ".join(prompt)}, quiet=quiet, field="image")


@app.command()
def edit(image_path: str, prompt: Annotated[list[str], typer.Argument()], quiet: bool = False) -> None:
    """Edit an image from an instruction."""
    _run_job("edit", {"image": image_path, "prompt": " ".join(prompt)}, quiet=quiet, field="image")


@app.command()
def tts(text: Annotated[list[str], typer.Argument()], voice: str | None = None, quiet: bool = False) -> None:
    """Speak text to a wav file."""
    payload: dict[str, Any] = {"text": " ".join(text)}
    if voice:
        payload["voice"] = voice
    _run_job("tts", payload, quiet=quiet, field="audio")


@app.command()
def stt(audio: str, quiet: bool = False) -> None:
    """Transcribe an audio file."""
    _run_job("stt", {"audio": audio}, quiet=quiet, field="text")


@app.command()
def song(prompt: Annotated[list[str], typer.Argument()], duration: int = 60, quiet: bool = False) -> None:
    """Make a song from a theme, genre/vibe and duration; lyrics are written unless given after 'lyrics:'."""
    _run_job("song", {"prompt": " ".join(prompt), "duration": duration}, quiet=quiet, field="audio")


@app.command()
def film(prompt: Annotated[list[str], typer.Argument()], duration: int = 20, quiet: bool = False) -> None:
    """Make a short film: shots planned from the request (or a 'script:' section), rendered, scored and cut."""
    _run_job("film", {"prompt": " ".join(prompt), "duration": duration}, quiet=quiet, field="video")


@app.command()
def music(prompt: Annotated[list[str], typer.Argument()], duration: int = 30, quiet: bool = False) -> None:
    """Render music exactly as described (no lyric writing); see `law song` for full songs."""
    _run_job("music", {"caption": " ".join(prompt), "duration": duration}, quiet=quiet, field="audio")


@app.command()
def video(prompt: Annotated[list[str], typer.Argument()],
          image_path: Annotated[str | None, typer.Option("--image")] = None,
          seconds: Annotated[int, typer.Option("--seconds", help="clip length at 16 fps")] = 5,
          quiet: bool = False) -> None:
    """Generate a video from text, or animate an image with --image."""
    payload: dict[str, Any] = {"prompt": " ".join(prompt), "frames": max(17, seconds * 16 + 1)}
    kind = "video"
    if image_path:
        payload["image"] = image_path
        kind = "animate"
    _run_job(kind, payload, quiet=quiet, field="video")


@app.command()
def search(query: Annotated[list[str], typer.Argument()], quiet: bool = False) -> None:
    """Web search via SearXNG."""
    _run_job("search", {"query": " ".join(query)}, quiet=quiet)


@app.command()
def jobs(limit: int = 20) -> None:
    """Recent jobs."""
    data = httpx.get(f"{_server()}/jobs", params={"limit": limit}, timeout=10).json()
    table = Table("id", "kind", "status", "source", "created")
    for j in data:
        import datetime as dt
        when = dt.datetime.fromtimestamp(j["created"]).strftime("%H:%M:%S")
        table.add_row(j["id"], j["kind"], j["status"], j["source"], when)
    con.print(table)


@app.command()
def cancel(job_id: str) -> None:
    """Cancel a job."""
    r = httpx.post(f"{_server()}/jobs/{job_id}/cancel", timeout=10)
    con.print(r.json())


# ---- platform -------------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Show the version."""
    con.print(f"law {__version__}")


@app.command()
def serve() -> None:
    """Run the job API (used by the com.law6.api service)."""
    from law.api import serve as serve_api
    serve_api(_paths())


@app.command()
def worker() -> None:
    """Run the job worker (used by the com.law6.worker service)."""
    from law.worker_main import main
    main(_paths())


@app.command()
def telegram() -> None:
    """Run the Telegram bot (used by the com.law6.telegram service)."""
    from law.telegram_bot import main
    main(_paths())


@app.command("eval")
def eval_cmd(only: Annotated[str | None, typer.Option("--only", help="case ids or domains, comma-separated")] = None
             ) -> None:
    """Run the golden evaluation set through the real product and print a scored report."""
    from law.evals import markdown, run_suite
    paths = _paths()
    results = run_suite(paths.trained / "evals" / "golden.yaml", _server(),
                        paths.outputs / "test-artifacts" / "evals", set(only.split(",")) if only else None)
    con.print(markdown(results))
    raise typer.Exit(0 if all(r.passed for r in results) else 1)


@app.command("mcp-mac")
def mcp_mac() -> None:
    """Run the Mac app-control MCP server over stdio (started by OpenCode for the operator role)."""
    from law.mcp_mac import main
    main()


@app.command()
def install(dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
            secrets_file: Annotated[str | None, typer.Option("--secrets")] = None,
            profile: str = "full",
            only: Annotated[str | None, typer.Option("--only", help="comma-separated step names")] = None) -> None:
    """Install or update LAW to match the registry (safe to run repeatedly)."""
    from law import registry, secrets
    from law.install.engine import LockedError, run
    from law.install.steps import Ctx, default_steps
    paths = _paths()
    imported = secrets.read(Path(secrets_file)) if secrets_file else None
    ctx = Ctx(paths, registry.load(paths.registry_file), __import__("law.sysapi", fromlist=["SysAPI"]).SysAPI(),
              interactive=not dry_run, imported_secrets=imported, profile=profile)
    try:
        report = run(paths, default_steps(ctx), dry_run=dry_run, console=con,
                     only=set(only.split(",")) if only else None)
    except LockedError as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    con.print(f"\n[bold]{'would change' if dry_run else 'changed'} {report.changes} item(s); "
              f"{'ok' if report.ok else 'problems remain'}[/]")
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def doctor() -> None:
    """Check that everything is healthy and print the LAN URLs."""
    from law.doctor import run_doctor
    raise typer.Exit(0 if run_doctor(_paths(), con) else 1)


@app.command()
def status() -> None:
    """Short status line."""
    try:
        h = httpx.get(f"{_server()}/healthz", timeout=5).json()
        con.print(f"api [green]up[/]  queued={h.get('queued')} running={h.get('running')}")
    except (httpx.HTTPError, OSError):
        con.print("api [red]down[/]")


def _svc_label(name: str) -> str:
    from law import registry
    reg = registry.load(_paths().registry_file)
    svc_def = reg.services.get(name)
    if not svc_def or not svc_def.label:
        raise typer.BadParameter(f"unknown service {name!r}")
    return svc_def.label


@svc.command("restart")
def svc_restart(name: str) -> None:
    """Restart a service."""
    from law.sysapi import SysAPI
    SysAPI().launchd_kickstart(_svc_label(name))
    con.print(f"restarted {name}")


@svc.command("logs")
def svc_logs(name: str, lines: int = 40) -> None:
    """Tail a service log."""
    log = _paths().logs / f"{name}.log"
    if not log.exists():
        err.print(f"no log at {log}")
        raise typer.Exit(1)
    con.print(log.read_text()[-8000:])


@models_cli.command("list")
def models_list() -> None:
    """List registry models and whether they are downloaded."""
    from law import registry
    paths = _paths()
    reg = registry.load(paths.registry_file)
    table = Table("id", "lane", "disk", "present")
    for m in reg.llm_models:
        present = (paths.llm_models / m.id / ".law-complete").exists()
        table.add_row(m.id, m.lane, f"{m.disk_gb} GB", "[green]yes[/]" if present else "[red]no[/]")
    con.print(table)


secrets_cli = typer.Typer(no_args_is_help=True, help="set/export your Telegram and Hugging Face tokens")
app.add_typer(secrets_cli, name="secrets")


@app.command()
def save(commit: Annotated[bool, typer.Option("--commit/--no-commit")] = True) -> None:
    """Export Open WebUI presets, prompts, tools and knowledge definitions into the repo (secret-scanned)."""
    import subprocess

    from law import registry
    from law.state_sync import OpenWebUI, StateError
    from law.state_sync import save as do_save
    p = _paths()
    reg = registry.load(p.registry_file)
    target = p.repo / "settings" / "openwebui"
    try:
        files = do_save(OpenWebUI(f"http://127.0.0.1:{reg.ports.open_webui}"), target, p.tmp / "openwebui-export")
    except StateError as exc:
        con.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    con.print(f"saved {len(files)} files to {target}")
    if commit:
        subprocess.run(["git", "-C", str(p.repo), "add", str(target)], check=True)
        if subprocess.run(["git", "-C", str(p.repo), "diff", "--cached", "--quiet", "--", str(target)]).returncode:
            subprocess.run(["git", "-C", str(p.repo), "commit", "-m", "state: save Open WebUI settings", "--",
                            str(target)], check=True)
            con.print("committed (not pushed)")
        else:
            con.print("no changes to commit")


@app.command("restore-state")
def restore_state() -> None:
    """Add the repo's saved Open WebUI presets, prompts, tools and knowledge bases that are missing here."""
    from law import registry
    from law.state_sync import OpenWebUI, load
    p = _paths()
    reg = registry.load(p.registry_file)
    added = OpenWebUI(f"http://127.0.0.1:{reg.ports.open_webui}").restore(load(p.repo / "settings" / "openwebui"))
    con.print(", ".join(f"{k}: {v} added" for k, v in added.items()))


@app.command()
def reclaim(dry_run: Annotated[bool, typer.Option("--dry-run")] = False) -> None:
    """Free disk space: unused model caches, old trash/logs/temp files, tool caches."""
    from law.ops import reclaim as do_reclaim
    from law.sysapi import SysAPI
    do_reclaim(_paths(), SysAPI(), con, dry_run=dry_run)


@app.command()
def uninstall(system: Annotated[bool, typer.Option("--system", help="also remove the ~/.zprofile block")] = False,
              yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    """Stop and remove LAW services and containers (data in ~/LAW is kept)."""
    from law.ops import uninstall as do_uninstall
    from law.sysapi import SysAPI
    if not yes and not typer.confirm("Stop and remove all LAW services?"):
        raise typer.Exit(1)
    do_uninstall(_paths(), SysAPI(), con, system=system)


@secrets_cli.command("set")
def secrets_set_cmd(key: str, value: str) -> None:
    """Set LAW_TELEGRAM_TOKEN, LAW_TELEGRAM_USER_IDS or HF_TOKEN, then run `law install` to apply."""
    from law.ops import secrets_set
    try:
        secrets_set(_paths(), key, value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    con.print(f"{key} saved to ~/LAW/state/.env; run `law install` to apply")


@secrets_cli.command("export")
def secrets_export_cmd(dest: Path) -> None:
    """Write your user-provided tokens to a file to keep in a password manager (for a fresh Mac)."""
    from law.ops import secrets_export
    secrets_export(_paths(), dest)
    con.print(f"wrote {dest} (mode 600). Keep it private; reinstall with ./install.sh --secrets {dest}")


if __name__ == "__main__":
    app()
