"""Phase 3 tests: server-error explanations, probe fixes, Anthropic API, undo,
add-model / remove-model, registry overrides."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from law import cli
from law.modelcmds import ModelCmdError, add_model, default_id, guess_kind, guess_parsers, remove_model
from law.paths import get_paths
from law.registry import load
from law.runtime import anthropic as anth
from law.runtime.gateway import Gateway
from law.runtime.modelproc import PromptTooSlow, explain_server_error
from law.runtime.probe import verdict
from law.undo import Undo, UndoError
from law.util import atomic_write

from test_phase1 import chat, free_port, run_one
from test_phase1 import home  # noqa: F401  (fixture)
from test_phase2 import make_rt, probe

GIT = shutil.which("git") is not None


# ── the 64K failure: explained, not retried ─────────────────────────────
REAL_LOG = """BatchScheduler admitted uid=1 (active=1, prompt_tokens=84927, cached_prefix=0, segments=2)
    response = await asyncio.wait_for(result_queue.get(), timeout=self._rpc_timeout)
TimeoutError
"""


def test_explain_server_error_from_real_log(tmp_path):
    f = tmp_path / "model.log"
    f.write_text(REAL_LOG)
    e = explain_server_error("{'message': '', 'code': '500'}", f)
    assert isinstance(e, PromptTooSlow) and e.retry is False
    assert "300s" in str(e) and "84,927" in str(e)
    f.write_text("something else broke\nTraceback: KeyError\n")
    e2 = explain_server_error("boom", f)
    assert not isinstance(e2, PromptTooSlow) and "KeyError" in str(e2)


def test_explain_server_error_reads_only_this_requests_log(tmp_path):
    """A stale earlier request left a genuine length error in the log; THIS request then crashed
    on the chat template. Reading from the offset must surface the template crash, not the old
    length error (handover §6.1)."""
    from law.runtime.modelproc import log_size
    f = tmp_path / "model.log"
    # an earlier request that really was too long (prompt_tokens + a length phrase):
    f.write_text("admitted prompt_tokens=84927\nmaximum context length exceeded\n")
    since = log_size(f)
    with f.open("a") as fh:                               # this request's real failure:
        fh.write("ValueError: Can only get item pairs from a mapping\n")
    e = explain_server_error("HTTP 500", f, since)
    assert not isinstance(e, PromptTooSlow) and "mapping" in str(e)   # real cause, from the offset
    e_bad = explain_server_error("HTTP 500", f, 0)                    # old behaviour would misread it
    assert isinstance(e_bad, PromptTooSlow)


@pytest.mark.parametrize("http", [False, True])
def test_prompt_too_slow_fails_once_with_reason(home, monkeypatch, http):
    monkeypatch.setenv("FAKE_500_OVER_CHARS", "500")
    if http:
        monkeypatch.setenv("FAKE_500_HTTP", "1")
    w, _ = make_rt()
    chat(w.q, text="x" * 2000)
    job = run_one(w)
    assert job["status"] == "failed" and job["attempts"] == 1
    assert "gave up after 300s" in job["error"]
    assert w.mm.alive()                                   # model kept loaded — it isn't broken
    chat(w.q, text="hi")
    assert run_one(w)["status"] == "done"
    w.mm.stop()


# ── probe fixes ─────────────────────────────────────────────────────────
def test_probe_records_time_and_reason_on_server_error(home, monkeypatch):
    w, _, _, job = probe(home, monkeypatch, [512, 4096], env={"FAKE_500_OVER_CHARS": 8000})
    res = json.loads(job["result"])["probe"]
    bad = res["sizes"][-1]
    assert bad["size"] == 4096 and bad["safe"] is False and bad["reason"] == "slow"
    assert isinstance(bad["seconds"], float) and "300s" in bad["error"]
    assert res["proven_context"] == 512 and res["suggested_context"] == 512


def test_probe_counts_slow_prompts_as_unsafe(home, monkeypatch):
    w, _, _, job = probe(home, monkeypatch, [256, 512], env={"FAKE_SECRET_SLEEP": 0.6}, max_prompt_s=0.3)
    res = json.loads(job["result"])["probe"]
    assert res["sizes"][0]["reason"] == "slow" and res["proven_context"] is None


def test_probe_uses_real_token_counts(home, monkeypatch):
    w, _, _, job = probe(home, monkeypatch, [1024, 2048], env={"FAKE_USAGE": 1})
    res = json.loads(job["result"])["probe"]
    first, second = res["sizes"]
    assert first["prompt_tokens"] and second["prompt_tokens"]
    # after calibrating on the first size, the second lands close to the requested size
    assert abs(second["prompt_tokens"] - 2048) / 2048 < 0.1


def test_verdict_rules():
    assert verdict(65536, 32768, None, 32768)[0] is None                 # untested above → no change
    assert "above what was tested" in verdict(65536, 32768, None, 32768)[1]
    assert verdict(65536, 32768, 65536, 65536)[0] == 32768               # failed at ≤ context → lower
    assert verdict(32768, 65536, None, 65536)[0] == 65536                # proven more → can raise
    assert verdict(32768, 32768, 65536, 65536)[0] is None                # within proven → keep
    assert verdict(32768, None, 4096, 4096)[0] is None


# ── Anthropic conversion ────────────────────────────────────────────────
def test_anthropic_request_conversion():
    req = {"model": "claude-sonnet-4-5", "max_tokens": 500, "system": [{"type": "text", "text": "be brief"}],
           "stop_sequences": ["END"], "temperature": 0.2,
           "tools": [{"name": "read_file", "description": "read", "input_schema": {
               "type": "object", "properties": {"path": {"type": "string"}}}}],
           "tool_choice": {"type": "any"},
           "messages": [
               {"role": "user", "content": [{"type": "text", "text": "open a.txt"},
                                            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                         "data": "AAA"}}]},
               {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"},
                                                 {"type": "text", "text": "sure"},
                                                 {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                                                  "input": {"path": "a.txt"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                             "content": [{"type": "text", "text": "hello"}]},
                                            {"type": "text", "text": "summarise"}]}]}
    msgs, params = anth.to_openai(req)
    assert msgs[0] == {"role": "system", "content": "be brief"}
    assert msgs[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAA"
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a.txt"}'}
    assert msgs[2]["content"] == "sure"
    assert msgs[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "hello"}
    assert msgs[4] == {"role": "user", "content": "summarise"}
    assert params["tool_choice"] == "required" and params["stop"] == ["END"] and params["max_tokens"] == 500
    assert params["tools"][0]["function"]["parameters"]["properties"]["path"]["type"] == "string"
    with pytest.raises(anth.AnthropicError, match="max_tokens"):
        anth.to_openai({"messages": [{"role": "user", "content": "x"}]})


def test_anthropic_response_and_stream_translation():
    res = {"message": {"content": "hi", "tool_calls": [{"id": "c1", "function": {
        "name": "f", "arguments": '{"a": 1}'}}]}, "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    m = anth.from_openai(res, "claude-x", "msg_1")
    assert m["content"][1] == {"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}}
    assert m["stop_reason"] == "tool_use" and m["usage"] == {"input_tokens": 7, "output_tokens": 3}
    tr = anth.StreamTranslator("claude-x", "msg_1")
    ev = list(tr.start()) + list(tr.delta({"content": "He"})) + list(tr.delta({"content": "llo"}))
    ev += list(tr.delta({"tool_calls": [{"index": 0, "id": "c9", "function": {"name": "f", "arguments": ""}}]}))
    ev += list(tr.delta({"tool_calls": [{"index": 0, "function": {"arguments": '{"a":1}'}}]}))
    ev += list(tr.finish("tool_calls"))
    names = [n for n, _ in ev]
    assert names == ["message_start", "content_block_start", "content_block_delta", "content_block_delta",
                     "content_block_stop", "content_block_start", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    assert ev[5][1]["content_block"]["id"] == "c9" and ev[5][1]["index"] == 1
    assert ev[8][1]["delta"]["stop_reason"] == "tool_use"


def test_anthropic_model_mapping():
    reg = load()
    rules = {"haiku": "general", "*": "coder"}
    r = lambda n: anth.resolve_model(n, reg.personas, reg.models, rules, "general")
    assert r("claude-3-5-haiku-latest") == "general"
    assert r("claude-sonnet-4-5") == "coder"
    assert r("writer") == "writer" and r("qwen38-27b") == "qwen38-27b"


# ── Anthropic endpoint through the queue ────────────────────────────────
@pytest.fixture
def agw(home):
    w, _ = make_rt()
    th = threading.Thread(target=w.run_forever, daemon=True)
    th.start()
    port = free_port()
    srv = Gateway(w.paths, w.reg, w.q).serve("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", w
    srv.shutdown(); w.stop_event.set(); th.join(10)


def apost(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": "local",
                                          "anthropic-version": "2023-06-01"})
    return urllib.request.urlopen(req, timeout=30)


def test_messages_non_stream(agw):
    base, _ = agw
    r = json.loads(apost(base + "/v1/messages", {"model": "claude-sonnet-4-5", "max_tokens": 100,
                                                 "messages": [{"role": "user", "content": "hi"}]}).read())
    assert r["type"] == "message" and r["content"][0]["text"] == "Hello from qwen3-coder-next"
    assert r["model"] == "claude-sonnet-4-5" and r["stop_reason"] == "end_turn"
    r2 = json.loads(apost(base + "/v1/messages", {"model": "claude-haiku-4-5", "max_tokens": 50,
                                                  "messages": [{"role": "user", "content": "hi"}]}).read())
    assert r2["content"][0]["text"] == "Hello from qwen38-27b"


def test_messages_stream_with_tool_use(agw):
    base, _ = agw
    resp = apost(base + "/v1/messages", {"model": "claude-sonnet-4-5", "max_tokens": 100, "stream": True,
                                         "messages": [{"role": "user", "content": "TOOL"}]})
    events, cur = [], None
    for raw in resp:
        line = raw.decode().strip()
        if line.startswith("event: "):
            cur = line[7:]
        elif line.startswith("data: "):
            events.append((cur, json.loads(line[6:])))
            if cur == "message_stop":
                break
    names = [n for n, _ in events if n != "ping"]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    start = next(d for n, d in events if n == "content_block_start")
    assert start["content_block"]["type"] == "tool_use" and start["content_block"]["name"] == "get_time"
    args = "".join(d["delta"]["partial_json"] for n, d in events
                   if n == "content_block_delta" and d["delta"]["type"] == "input_json_delta")
    assert json.loads(args) == {"tz": "UTC"}
    md = next(d for n, d in events if n == "message_delta")
    assert md["delta"]["stop_reason"] == "tool_use"


def test_messages_errors_and_count_tokens(agw):
    base, _ = agw
    with pytest.raises(urllib.error.HTTPError) as e:
        apost(base + "/v1/messages", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert e.value.code == 400 and json.loads(e.value.read())["type"] == "error"
    n = json.loads(apost(base + "/v1/messages/count_tokens",
                         {"model": "x", "messages": [{"role": "user", "content": "hello " * 100}]}).read())
    assert n["input_tokens"] > 100


# ── undo ────────────────────────────────────────────────────────────────
@pytest.mark.skipif(not GIT, reason="git not installed")
def test_snapshot_rollback_is_itself_undoable(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("v1")
    (proj / ".gitignore").write_text("secret.env\n")
    (proj / "secret.env").write_text("TOKEN=1")
    u = Undo(proj)
    s1, n, _ = u.snapshot("before agent")
    assert n == 2                                            # a.txt + .gitignore (secret.env ignored)
    assert u.snapshot("again")[1] == 0                       # nothing changed → no new snapshot
    (proj / "a.txt").write_text("v2")
    (proj / "b.txt").write_text("new")
    res = u.rollback()
    assert (proj / "a.txt").read_text() == "v1" and not (proj / "b.txt").exists()
    assert (proj / "secret.env").read_text() == "TOKEN=1"    # ignored files untouched
    assert set(res["changed"]) == {"a.txt", "b.txt"}
    u.rollback(res["safety"])                                # undo the undo
    assert (proj / "a.txt").read_text() == "v2" and (proj / "b.txt").read_text() == "new"
    assert u.rollback(res["safety"])["changed"] == []
    assert [s.message for s in u.list()][-1] == "before agent"


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_undo_never_touches_the_projects_own_git(tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / "x.py").write_text("print(1)")
    Undo(proj).snapshot()
    st = subprocess.run(["git", "status", "--porcelain"], cwd=proj, capture_output=True, text=True).stdout
    assert "?? x.py" in st and ".local-agent" not in st or "?? .local-agent/" in st
    log = subprocess.run(["git", "log", "--oneline"], cwd=proj, capture_output=True, text=True)
    assert log.returncode != 0 or log.stdout == ""          # user's repo still has no commits


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_undo_skips_big_files_and_bad_ids(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "big.bin").write_bytes(b"0" * 2_000_000)
    (proj / "s.txt").write_text("x")
    u = Undo(proj, max_file_mb=1)
    _, n, skipped = u.snapshot()
    assert skipped == ["big.bin"] and n == 1
    with pytest.raises(UndoError):
        u.rollback("not-an-id;rm")
    with pytest.raises(UndoError):
        Undo(tmp_path / "empty-never-snapshotted").rollback()


# ── add-model / remove-model ────────────────────────────────────────────
def fake_info(gb=8.5, tag="text-generation", tags=("mlx",), gated=False):
    return lambda repo, token: {"pipeline_tag": tag, "tags": list(tags), "gated": gated,
                                "cardData": {"license": "apache-2.0"},
                                "siblings": [{"rfilename": "model.safetensors", "size": int(gb * 1024**3)},
                                             {"rfilename": "README.md", "size": 100}]}


def test_add_model_and_remove(home):
    p = get_paths()
    res = add_model(p, "mlx-community/Qwen3-14B-4bit", fetch=fake_info(), persona="writer")
    e = res["entry"]
    assert e["id"] == "qwen3-14b" and e["weights_gb"] == 8.5 and e["tool_parser"] == "qwen3"
    assert e["reasoning_parser"] == "qwen3" and e["kind"] == "lm" and res["warnings"] == []
    reg = load(p)
    assert reg.models["qwen3-14b"].source == "user" and reg.personas["writer"]["model"] == "qwen3-14b"
    with pytest.raises(ModelCmdError, match="already registered"):
        add_model(p, "mlx-community/Qwen3-14B-4bit", mid="other", fetch=fake_info())
    with pytest.raises(ModelCmdError, match="persona"):
        remove_model(p, "qwen3-14b")
    data = yaml.safe_load(p.user_models.read_text())
    data.pop("personas")
    atomic_write(p.user_models, yaml.safe_dump(data))
    assert remove_model(p, "qwen3-14b")["repo"] == "mlx-community/Qwen3-14B-4bit"
    assert "qwen3-14b" not in load(p).models


def test_add_model_guards(home):
    p = get_paths()
    with pytest.raises(ModelCmdError, match="limit"):
        add_model(p, "big/Huge-Model", fetch=fake_info(gb=120))
    assert add_model(p, "big/Huge-Model", fetch=fake_info(gb=120), force=True)["entry"]["weights_gb"] == 120
    with pytest.raises(ModelCmdError, match="built-in"):
        add_model(p, "x/y", mid="qwen38-27b", fetch=fake_info())
    with pytest.raises(ModelCmdError, match="org/name"):
        add_model(p, "nonsense", fetch=fake_info())
    res = add_model(p, "someone/NotMlx", fetch=fake_info(tags=(), gated=True))
    assert len(res["warnings"]) == 2
    with pytest.raises(ModelCmdError, match="no weight files"):
        add_model(p, "someone/Empty", fetch=lambda r, t: {"siblings": []})


def test_guesses():
    assert guess_kind({"pipeline_tag": "image-text-to-text"}) == "vlm"
    assert guess_kind({"pipeline_tag": "feature-extraction"}) == "embed"
    assert guess_parsers("mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit") == ("qwen3_coder", None)
    assert guess_parsers("mlx-community/DeepSeek-R1-Distill-Qwen-32B-4bit") == (None, "deepseek_r1")
    assert default_id("mlx-community/Qwen3-14B-Instruct-4bit") == "qwen3-14b"


def test_override_builtin_keeps_tier_and_can_be_removed(home):
    p = get_paths()
    atomic_write(p.user_models, yaml.safe_dump({"models": [{"id": "qwen38-27b", "context": 40960}]}))
    m = load(p).models["qwen38-27b"]
    assert m.context == 40960 and m.tier == "core" and m.source == "override" and m.download == "auto"
    assert remove_model(p, "qwen38-27b")["was_override"] is True
    assert load(p).models["qwen38-27b"].context == 49152
    with pytest.raises(ModelCmdError, match="built-in"):
        remove_model(p, "qwen38-27b")


def test_cli_legacy_add_remove_now_available(home, capsys):
    assert cli.main(["--remove-model", "nope"]) == 1
    assert "no model 'nope'" in capsys.readouterr().err
