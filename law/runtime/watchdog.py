"""Watchdog (launchd service com.law.watchdog): a second pair of eyes on the worker
and the gateway. Checks every `interval_s`:

  worker  - no heartbeat for `heartbeat_stale_s` → the process is frozen or dead → restart
          - a job shows no progress for `stall_s` → stuck somewhere the worker's own
            timeouts can't reach → restart (the job goes back in the queue; a job that keeps
            getting interrupted is failed after max_interruptions so it can't loop)
          - skipped when the worker says "stopped" (you ran `law stop`)
  gateway - /health fails `gateway_failures` checks in a row → restart
  telegram- the bot (when configured) has not polled Telegram for `telegram.stale_s` → restart
  memory  - critical memory pressure while a job runs → a warning on that job
            (at most every 5 minutes)

Restarts use `launchctl kickstart -k`, at most once per `cooldown_s` per service.
Everything it does is recorded (`law status` shows the latest, `law logs watchdog` all).

Laptop sleep: when the Mac sleeps, every process (this one included) is frozen while the
wall clock keeps moving, so on wake every heartbeat looks stale purely because time jumped
— not because anything broke. The watchdog detects this as an over-large gap between its
own checks (`> interval_s + wake_gap_s`) and then holds OFF the staleness restarts (worker
heartbeat, stalled job, Telegram, gateway) for `wake_grace_s`, long enough for the services
to poll/heartbeat again on their own. Without this the bot got needlessly restarted after
every sleep (the symptom: "the Telegram bot has not polled for 3340s" overnight).
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from ..paths import get_paths
from ..registry import load
from ..util import run
from . import memory
from .db import Queue

log = logging.getLogger("law.watchdog")

DEFAULTS = {"interval_s": 15, "heartbeat_stale_s": 90, "stall_s": None, "gateway_failures": 3,
            "cooldown_s": 120, "pressure_warn_every_s": 300, "wake_gap_s": 60, "wake_grace_s": 120}


def _kickstart(label: str) -> None:
    """Restart a service — and REVIVE it if it has been booted out. `kickstart -k` silently
    no-ops on an unloaded job, so a service that got unloaded (sleep/wake churn, a failed
    bootstrap, a stray bootout) would never come back and the watchdog's whole point — being
    the backstop — would be lost. So: if loaded, kick it; if not, bootstrap its plist."""
    uid = os.getuid()
    loaded = run(["launchctl", "print", f"gui/{uid}/{label}"], check=False, timeout=30).returncode == 0
    if loaded:
        run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False, timeout=90)
        return
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not plist.exists():
        log.error("cannot restart %s: it is not loaded and %s is missing (run ./install.sh)", label, plist)
        return
    run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, timeout=60)   # clear any half-state
    run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=False, timeout=60)


class Watchdog:
    def __init__(self, reg, q: Queue, *, restart: Callable[[str], None] = _kickstart,
                 health: Callable[[], bool] | None = None, pressure: Callable[[], str] = memory.pressure,
                 now: Callable[[], float] = time.time, cfg: dict | None = None,
                 telegram_age: Callable[[], float | None] = lambda: None):
        rt = reg.raw.get("runtime") or {}
        self.cfg = {**DEFAULTS, **(rt.get("watchdog") or {}), **(cfg or {})}
        if not self.cfg["stall_s"]:
            wk = rt.get("worker") or {}
            ag = rt.get("agent") or {}
            self.cfg["stall_s"] = max(int(wk.get("first_token_timeout_s", 1800)),
                                      int(wk.get("load_timeout_s", 1800)),
                                      int(ag.get("shell_timeout_s", 0))) + 300
        self.tg_stale = float(((rt.get("telegram") or {}).get("stale_s")) or 300)
        self.telegram_age = telegram_age
        self.q, self.restart, self.pressure, self.now = q, restart, pressure, now
        port = reg.ports["gateway"]
        self.health = health or (lambda: _http_ok(f"http://127.0.0.1:{port}/health"))
        self.gw_fail = 0
        self.last_restart: dict[str, float] = {}
        self.last_pressure_warn = 0.0
        self.stop_event = threading.Event()
        self._last_tick: float | None = None     # self.now() at the previous check (for sleep detection)
        self._wake_until = 0.0                    # suppress staleness restarts until this time

    def _do(self, target: str, reason: str) -> bool:
        t = self.now()
        if t - self.last_restart.get(target, 0) < self.cfg["cooldown_s"]:
            return False
        self.last_restart[target] = t
        log.warning("restarting %s: %s", target, reason)
        self.q.log_watchdog(target, "restart", reason)
        self.restart(f"com.law.{target}")
        return True

    def check(self) -> list[str]:
        """One round of checks. Returns what was done (for tests and logs)."""
        done: list[str] = []
        now = self.now()
        # Sleep/wake (or a watchdog stall): a gap far larger than our own interval means
        # the whole machine was frozen while wall time advanced, so every heartbeat below is
        # stale only because time jumped. Hold off staleness restarts until the services have
        # had a grace window to check in again.
        gap = (now - self._last_tick) if self._last_tick is not None else 0.0
        self._last_tick = now
        if gap > self.cfg["interval_s"] + self.cfg["wake_gap_s"]:
            self._wake_until = now + self.cfg["wake_grace_s"]
            log.info("time jumped %.0fs (system sleep or a watchdog stall) — holding off restarts for %ss "
                     "while services check in again", gap, self.cfg["wake_grace_s"])
            self.q.log_watchdog("system", "wake", f"time jumped {gap:.0f}s; {self.cfg['wake_grace_s']:.0f}s grace")
            self.gw_fail = 0
        in_grace = now < self._wake_until

        w = self.q.worker_info()
        if w and w["state"] != "stopped":
            beat_age = now - (w["beat"] or 0)
            prog_age = now - (w.get("progress") or w["beat"] or 0)
            if beat_age > self.cfg["heartbeat_stale_s"] and not in_grace:
                if self._do("worker", f"no heartbeat for {beat_age:.0f}s (frozen or crashed)"):
                    done.append("restart worker (heartbeat)")
            elif w["state"] == "running" and prog_age > self.cfg["stall_s"] and not in_grace:
                if self._do("worker", f"job {w['job_id']} made no progress for {prog_age:.0f}s"):
                    done.append("restart worker (stalled job)")
            if w["state"] == "running" and w.get("job_id") and \
                    now - self.last_pressure_warn > self.cfg["pressure_warn_every_s"] and \
                    self.pressure() == "critical":
                self.last_pressure_warn = now
                self.q.add_event(w["job_id"], "warning", {"message": "macOS memory pressure is CRITICAL — "
                                                          "close big apps; the job may slow down."})
                self.q.log_watchdog("memory", "warn", f"critical pressure during job {w['job_id']}")
                done.append("pressure warning")
        tg = self.telegram_age()
        if tg is not None and tg > self.tg_stale and not in_grace:
            if self._do("telegram", f"the Telegram bot has not polled for {tg:.0f}s"):
                done.append("restart telegram")
        if self.health() or in_grace:
            self.gw_fail = 0                     # don't count health failures while services recover from a wake
        else:
            self.gw_fail += 1
            if self.gw_fail >= self.cfg["gateway_failures"]:
                if self._do("gateway", f"/health failed {self.gw_fail} checks in a row"):
                    done.append("restart gateway")
                self.gw_fail = 0
        return done

    def run_forever(self) -> None:
        log.info("watchdog ready (every %ss, stall after %ss)", self.cfg["interval_s"], self.cfg["stall_s"])
        while not self.stop_event.wait(self.cfg["interval_s"]):
            try:
                self.check()
            except Exception as e:  # noqa: BLE001 — the watchdog itself must never die
                log.error("check failed: %s", e)


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s watchdog %(levelname)s %(message)s")
    paths = get_paths()
    from ..telegram import beat_age
    wd = Watchdog(load(paths), Queue(paths.queue_db), telegram_age=lambda: beat_age(paths))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: wd.stop_event.set())
    wd.run_forever()


if __name__ == "__main__":
    main()
