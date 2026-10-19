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
    worker = Worker(store, handlers(rt), capacity(rt), resolver(rt), models=models)
    return worker, store


async def _run(paths: LawPaths) -> None:
    worker, store = build_worker(paths)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await store.close()


def main(paths: LawPaths | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_run(paths or LawPaths.from_env()))


if __name__ == "__main__":
    main()
