"""`law probe <model>` — measure a model on THIS Mac, through the normal queue.

1. cold load      : unload whatever is loaded, start the model with a context comfortably
                    above the largest size tested, measure load time
2. speed          : one short prompt → time to first token, ≈ tokens/second
3. long context   : for each size (e.g. 4K → 128K tokens) hide a secret code in a long
                    text and ask for it back. Records pass/fail, the REAL prompt length
                    (when the server reports it), time, lowest free RAM and worst macOS
                    memory pressure. Stops at the first size that isn't safe.
4. verdict        : a size is SAFE when the code came back, at least `min_free_gb` RAM
                    stayed free, pressure never went critical, and the prompt was read in
                    under `max_prompt_s` (mlx-openai-server gives up after 300 s).
                    The probe then says what was PROVEN and only recommends a change when
                    the evidence supports it:
                      - your context is above a size that FAILED  → lower it to the proven size
                      - your context is above what was TESTED      → "untested", no change
                      - your context is within the proven range    → keep it (or raise it)

Results are saved to ~/.law/state/probes.json and shown by `law status` / `law probe --show`.
Nothing in the registry is changed automatically.
"""
from __future__ import annotations

import json
import random
import threading
import time

import psutil

from ..util import atomic_write
from . import memory
from .modelproc import ModelError, PromptTooSlow

COLORS = ["red", "blue", "green", "amber", "silver", "violet", "teal", "golden", "grey", "white"]
ANIMALS = ["fox", "heron", "otter", "lynx", "badger", "falcon", "tortoise", "ibis", "marten", "gecko"]
PLACES = ["river", "orchard", "harbour", "valley", "library", "market", "bridge", "meadow", "tower", "garden"]
# Measured with the Qwen tokenizer on an M5 Pro: 4,096 sentences = 84,927 tokens → ~20.7 per sentence.
# When the server reports prompt_tokens, later sizes are re-calibrated from the real number.
TOKENS_PER_SENTENCE = 20.7
DEFAULT_SIZES = [4096, 16384, 32768, 65536, 131072]


def haystack(tokens: int, code: str, depth: float = 0.7, seed: int = 7,
             per_sentence: float = TOKENS_PER_SENTENCE) -> str:
    rnd = random.Random(seed + tokens)
    n = max(4, int(tokens / per_sentence))
    sentences = [f"Note {i}: the {rnd.choice(COLORS)} {rnd.choice(ANIMALS)} walked {rnd.randint(2, 999)} "
                 f"steps toward the {rnd.choice(PLACES)}." for i in range(n)]
    sentences.insert(int(n * depth), f"IMPORTANT: the secret code is {code}. Remember it.")
    return " ".join(sentences)


def sentences_in(text: str) -> int:
    return text.count("Note ") + 1


class Sampler:
    """Background sampler: lowest free RAM, worst pressure, peak RSS of the model process tree."""

    def __init__(self, pid: int | None, pressure=memory.pressure, avail=memory.available_gb):
        self.pid, self.pressure_fn, self.avail_fn = pid, pressure, avail
        self.min_avail, self.worst, self.peak_rss = float("inf"), "normal", 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _rss(self) -> float:
        try:
            p = psutil.Process(self.pid)
            return sum(x.memory_info().rss for x in [p, *p.children(recursive=True)]) / 1024**3
        except (psutil.Error, TypeError):
            return 0.0

    def sample(self) -> None:
        self.min_avail = min(self.min_avail, self.avail_fn())
        self.worst = memory.worse(self.worst, self.pressure_fn())
        self.peak_rss = max(self.peak_rss, self._rss())

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self.sample()

    def __enter__(self):
        self.sample()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(2)
        self.sample()

    def summary(self) -> dict:
        return {"min_free_gb": round(self.min_avail, 1), "pressure": self.worst,
                "model_rss_gb": round(self.peak_rss, 1)}


def judge(row: dict, min_free: float, max_prompt_s: float) -> str | None:
    """None if safe, otherwise the main reason it isn't."""
    if not row["ok"]:
        return "slow" if row.get("too_slow") else "error"
    if not row["recall"]:
        return "recall"
    if row["pressure"] == "critical":
        return "pressure"
    if row["min_free_gb"] < min_free:
        return "memory"
    if row.get("seconds", 0) > max_prompt_s:
        return "slow"
    return None


REASON_TEXT = {"error": "the model server returned an error",
               "slow": "reading the prompt took too long (server limit 300 s)",
               "recall": "the model could not find the code (answer quality breaks down)",
               "pressure": "macOS memory pressure went critical",
               "memory": "too little RAM stayed free"}


def verdict(registry_ctx: int | None, proven: int | None, failed_at: int | None,
            tested_max: int) -> tuple[int | None, str]:
    """→ (context to recommend or None, explanation)."""
    cur = registry_ctx or 0
    if failed_at is not None and cur >= failed_at:
        if proven:
            return proven, (f"your context {cur} is at or above a size that FAILED ({failed_at}); "
                            f"lower it to the proven {proven}")
        return None, (f"even the smallest size failed — this model isn't usable for long prompts "
                      f"on this Mac right now")
    if proven is None:
        return None, "no size passed"
    if cur > tested_max:
        return None, (f"proven up to {proven}; your context {cur} is above what was tested — "
                      f"no change recommended (test it with --sizes {cur // 1024}k)")
    if proven > cur and failed_at is None:
        return proven, f"proven up to {proven}, above your context {cur} — you can raise it"
    if proven > cur:
        return proven, (f"proven up to {proven} (failed at {failed_at}), above your context {cur} — "
                        f"you can raise it to {proven}")
    return None, f"your context {cur} is within the proven range — keep it"


def run_probe(w, job: dict) -> dict:
    jid = job["id"]
    payload = json.loads(job["payload"])
    mid = job["model"]
    m = w.reg.model(mid)
    pcfg = (w.reg.raw.get("runtime") or {}).get("probe") or {}
    sizes = sorted(int(s) for s in (payload.get("sizes") or pcfg.get("sizes") or DEFAULT_SIZES))
    min_free = float(payload.get("min_free_gb", pcfg.get("min_free_gb", 4)))
    max_prompt_s = float(payload.get("max_prompt_s", pcfg.get("max_prompt_s", 240)))
    ctx = max(int(max(sizes) * 1.15) + 4096, m.context or 0)
    say = lambda msg, **kw: w.q.add_event(jid, "probe", {"message": msg, **kw})

    out: dict = {"model": mid, "repo": m.repo, "ts": time.time(), "context_tested": ctx,
                 "registry_context": m.context, "sizes": [], "max_prompt_s": max_prompt_s,
                 "min_free_gb_required": min_free}

    # 1. cold load
    say(f"cold load of {mid} with a {ctx}-token context")
    w.check(jid)
    w.mm.stop()
    t0 = time.time()
    with Sampler(None, w.pressure, w.mm.free_gb) as s_load:
        base = w.mm.ensure(mid, context=ctx)
        s_load.pid = w.mm.proc.pid if w.mm.proc else None
    out["load_s"] = round(time.time() - t0, 1)
    out["load"] = s_load.summary()
    say(f"loaded in {out['load_s']}s", step="load", **out["load"])

    # 2. speed
    w.check(jid)
    with Sampler(w.mm.proc.pid if w.mm.proc else None, w.pressure, w.mm.free_gb) as s_speed:
        r = w.complete(jid, base, [{"role": "user", "content":
                                    "Write a 150-word paragraph about the ocean. No preamble."}],
                       {"max_tokens": 400, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}},
                       publish=False)
    gen = max(r["gen_s"], 1e-6)
    out["speed"] = {"ttft_s": round(r["ttft_s"] or 0, 2), "tok_s": round(r["n_deltas"] / gen, 1),
                    "tokens": r["n_deltas"], **s_speed.summary()}
    say(f"speed: ~{out['speed']['tok_s']} tokens/s, first token after {out['speed']['ttft_s']}s",
        step="speed", **out["speed"])

    # 3. long context
    per_sentence = TOKENS_PER_SENTENCE
    proven, failed_at, failed_reason = None, None, None
    for size in sizes:
        w.check(jid)
        code = f"LAW-{random.Random(size).randint(100000, 999999)}"
        text = haystack(size, code, per_sentence=per_sentence)
        msgs = [{"role": "user", "content": text
                 + "\n\nQuestion: what is the secret code? Reply with the code only."}]
        say(f"long context: ~{size} tokens…", step="context", size=size)
        row: dict = {"size": size}
        t1 = time.time()
        with Sampler(w.mm.proc.pid if w.mm.proc else None, w.pressure, w.mm.free_gb) as smp:
            try:
                # thinking off: a thinking model would spend the small token budget reasoning and
                # never write the code (templates without the switch ignore the kwarg). The
                # reasoning text is checked too, for models that think regardless.
                r = w.complete(jid, base, msgs, {"max_tokens": 64, "temperature": 0,
                                                 "chat_template_kwargs": {"enable_thinking": False}},
                               publish=False)
                reply = r["result"]["message"]
                answer = ((reply.get("content") or "") + " " + (reply.get("reasoning_content") or "")).strip()
                pt = (r["result"].get("usage") or {}).get("prompt_tokens")
                row.update(ok=True, recall=code in answer, prompt_tokens=pt)
                if pt:                                   # calibrate the next sizes to the real tokenizer
                    per_sentence = pt / sentences_in(text)
            except ModelError as e:
                row.update(ok=False, recall=False, error=str(e)[:400], too_slow=isinstance(e, PromptTooSlow))
        row["seconds"] = round(time.time() - t1, 1)
        row.update(smp.summary())
        reason = judge(row, min_free, max_prompt_s)
        row["safe"], row["reason"] = reason is None, reason
        out["sizes"].append(row)
        say(f"~{size} tokens: " + ("OK" if reason is None else f"NOT OK — {REASON_TEXT[reason]}") +
            f" (recall {'yes' if row['recall'] else 'no'}, {row['seconds']}s, "
            f"min free {row['min_free_gb']} GB, pressure {row['pressure']})", step="context_result", **row)
        if reason is None:
            proven = size
            continue
        failed_at, failed_reason = size, reason
        if not row["ok"]:
            w.mm.stop()                 # the server may be wedged after a failure
        break

    tested_max = out["sizes"][-1]["size"] if out["sizes"] else 0
    rec, why = verdict(m.context, proven, failed_at, tested_max)
    out.update(proven_context=proven, failed_at=failed_at, failed_reason=failed_reason,
               suggested_context=rec, recommendation=why)
    _save(w.paths, mid, out)

    lines = [f"Probe of {mid}",
             f"- load: {out['load_s']}s",
             f"- speed: ~{out['speed']['tok_s']} tokens/s (first token {out['speed']['ttft_s']}s)"]
    for row in out["sizes"]:
        real = f" (real {row['prompt_tokens']:,} tokens)" if row.get("prompt_tokens") else ""
        lines.append(f"- ~{row['size']} tokens{real}: {'OK' if row['safe'] else 'NOT OK'} · "
                     f"{row['seconds']}s · recall {'yes' if row['recall'] else 'no'} · "
                     f"min free {row['min_free_gb']} GB · pressure {row['pressure']}"
                     + (f" · {REASON_TEXT[row['reason']]}" if row.get("reason") else ""))
        if row.get("error"):
            lines.append(f"    {row['error']}")
    lines.append(f"- proven safe up to: {proven or 'nothing'}"
                 + (f" · first failure at {failed_at}" if failed_at else
                    f" · nothing above {tested_max} tested"))
    lines.append(f"- verdict: {why}")
    w.mm.stop()                         # next job loads it again with the normal context
    return {"message": {"role": "assistant", "content": "\n".join(lines)}, "finish_reason": "stop",
            "probe": out}


def _save(paths, mid: str, data: dict) -> None:
    f = paths.probes
    allp = load_probes(paths)
    allp[mid] = data
    atomic_write(f, json.dumps(allp, indent=2, sort_keys=True) + "\n")


def load_probes(paths) -> dict:
    try:
        return json.loads(paths.probes.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
