"""Worker process entry point: load the registry, connect to Valkey, run the queue worker."""

from __future__ import annotations

import asyncio
import logging
import signal

from law.api import redis_url
from law.app import capacity, handlers, resolver
from law.handlers.base import Runtime
from law.llm import OmlxModelControl
from law.paths import LawPaths
from law.queue.store import JobStore
from law.queue.worker import Worker

log = logging.getLogger("law.worker")


def build_worker(paths: LawPaths) -> tuple[Worker, JobStore]:
    rt = Runtime.load(paths)
    store = JobStore.connect(redis_url(rt.reg))
    light = {m.id for m in rt.reg.llm_models if m.lane == "light"}
    internal = f"http://{rt.reg.network.loopback}:{rt.reg.ports.llm_internal}"
    models = OmlxModelControl(internal, light)
    from law.install.steps import WORKER_RESTART_FLAG

    worker = Worker(store, handlers(rt), capacity(rt), resolver(rt), models=models,
                    restart_flag=paths.state / WORKER_RESTART_FLAG)
    return worker, store


PRUNE_EVERY_S = 6 * 3600


async def prune_once(paths: LawPaths, store: JobStore) -> tuple[int, int]:
    """Apply the retention settings (read fresh, so Console edits take effect): old finished jobs and old agent
    conversations. Returns (jobs, sessions) deleted."""
    import time

    from law import registry, storage

    keep = registry.load(paths.registry_file).retention
    jobs = await store.prune(time.time() - keep.jobs_days * 86400)
    sessions = await asyncio.to_thread(storage.prune_sessions, paths, keep.agent_sessions_days)
    return jobs, sessions


async def _prune_forever(paths: LawPaths, store: JobStore) -> None:
    while True:
        try:
            jobs, sessions = await prune_once(paths, store)
            logging.getLogger("law.worker").info("retention: pruned %s jobs, %s agent sessions", jobs, sessions)
        except Exception:  # never let housekeeping take the worker down
            logging.getLogger("law.worker").exception("retention prune failed")
        await asyncio.sleep(PRUNE_EVERY_S)


async def _run(paths: LawPaths) -> None:
    worker, store = build_worker(paths)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    pruner = asyncio.create_task(_prune_forever(paths, store))
    try:
        await worker.run()
    finally:
        pruner.cancel()
        await store.close()


def main(paths: LawPaths | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_run(paths or LawPaths.from_env()))


if __name__ == "__main__":
    main()
