"""Exhaustive tests of the pure scheduling policy."""

from __future__ import annotations

import random

from law.queue.jobs import Job, Priority
from law.queue.scheduler import Capacity, Running, effective_class, pick

CAP = Capacity(budget_gb=48, light_slots=3, none_slots=2, aging_s=300)
NOW = 1_000_000.0


def job(resource: str = "llm:a", lane: str = "heavy", mem: float = 30, prio: Priority = Priority.INTERACTIVE,
        age: float = 0.0) -> Job:
    j = Job(kind="ask", resource=resource, lane=lane, mem_gb=mem, priority=prio)  # type: ignore[arg-type]
    j.enqueued = NOW - age
    return j


def test_only_one_heavy_job_starts():
    q = [job(), job(), job()]
    started = pick(q, Running(), None, CAP, NOW)
    assert [j.lane for j in started] == ["heavy"]


def test_no_heavy_start_while_heavy_running():
    started = pick([job()], Running(heavy=job()), None, CAP, NOW)
    assert started == []


def test_priority_beats_affinity():
    batch_same_model = job("llm:resident", prio=Priority.BATCH)
    interactive_other = job("llm:other", prio=Priority.INTERACTIVE)
    started = pick([batch_same_model, interactive_other], Running(), "llm:resident", CAP, NOW)
    assert started[0] is interactive_other


def test_affinity_within_same_class_avoids_swap():
    other = job("llm:other", age=50)
    same = job("llm:resident", age=10)
    started = pick([other, same], Running(), "llm:resident", CAP, NOW)
    assert started[0] is same


def test_model_with_more_queued_work_wins_to_batch_swaps():
    a1 = job("llm:a", age=30)
    b1, b2, b3 = job("llm:b", age=5), job("llm:b", age=4), job("llm:b", age=3)
    started = pick([a1, b1, b2, b3], Running(), "llm:none", CAP, NOW)
    assert started[0].resource == "llm:b"


def test_aging_prevents_starvation():
    old_batch = job("llm:x", prio=Priority.BATCH, age=601)  # aged two classes -> interactive
    assert effective_class(old_batch, NOW, CAP.aging_s) == 0
    fresh_interactive = job("llm:resident", prio=Priority.INTERACTIVE, age=0)
    started = pick([fresh_interactive, old_batch], Running(), "llm:resident", CAP, NOW)
    # Same effective class: affinity picks the resident one first, but old_batch is not starved forever:
    assert started[0] is fresh_interactive
    started2 = pick([old_batch], Running(), "llm:resident", CAP, NOW)
    assert started2 == [old_batch]


def test_light_jobs_run_concurrently_with_heavy_within_budget():
    q = [job("tts", lane="light", mem=2) for _ in range(5)]
    started = pick(q, Running(heavy=job(mem=40)), "llm:a", CAP, NOW)
    assert len(started) == 3  # light slots
    started = pick(q, Running(heavy=job(mem=47)), "llm:a", CAP, NOW)
    assert started == []  # 47 + 2 > 48


def test_light_jobs_do_not_starve_a_waiting_heavy_job():
    heavy = job("llm:big", mem=46)
    lights = [job("stt", lane="light", mem=2) for _ in range(3)]
    started = pick([heavy, *lights], Running(), None, CAP, NOW)
    assert started[0] is heavy
    assert sum(j.mem_gb for j in started) <= CAP.budget_gb


def test_heavy_waits_when_running_lights_leave_no_room():
    started = pick([job(mem=47)], Running(light=[job("stt", "light", 2)]), None, CAP, NOW)
    assert started == []


def test_none_lane_capped():
    q = [job("", lane="none", mem=0) for _ in range(5)]
    assert len(pick(q, Running(), None, CAP, NOW)) == 2


def test_cancel_requested_jobs_never_start():
    j = job()
    j.cancel_requested = True
    assert pick([j], Running(), None, CAP, NOW) == []


def test_randomized_invariants():
    rng = random.Random(42)
    for _ in range(2000):
        q = []
        for _ in range(rng.randint(0, 12)):
            lane = rng.choice(["heavy", "light", "none"])
            mem = {"heavy": rng.uniform(5, 47), "light": rng.uniform(0.5, 4), "none": 0}[lane]
            q.append(job(rng.choice(["llm:a", "llm:b", "mflux:x", "tts"]), lane, mem,
                         Priority(rng.randint(0, 2)), rng.uniform(0, 1200)))
        running = Running()
        if rng.random() < 0.4:
            running.heavy = job(mem=rng.uniform(5, 40))
        running.light = [job("stt", "light", 1) for _ in range(rng.randint(0, 3))]
        started = pick(q, running, rng.choice([None, "llm:a", "llm:b"]), CAP, NOW)
        heavy_started = [j for j in started if j.lane == "heavy"]
        assert len(heavy_started) <= (0 if running.heavy else 1)
        assert running.used_gb() + sum(j.mem_gb for j in started) <= CAP.budget_gb + 1e-9
        assert len([j for j in started if j.lane == "light"]) + len(running.light) <= CAP.light_slots
        assert len({j.id for j in started}) == len(started)
