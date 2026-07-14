#!/usr/bin/env python3
"""
mlx-agent — persona-aware tool-executor for the local MLX workstation.

Runs NATIVELY on the host so it can actually touch the filesystem, shell, and
your SearXNG. Personas are DATA (a registry), not code: each persona is a name +
model + system prompt + allowed tools. The same generic tool loop runs any of
them, so you add a "researcher" or "pentester" without changing this file.

    your question → model picks a tool → THIS runs it for real →
    result fed back → model answers from real data.

Safety: shell is allowlisted (read-only auto-runs; anything else asks), file
paths are scoped, writes confirm. Personas default to the full toolset, but the
destructive/network confirmation gates ALWAYS apply — even to an "open" persona.

Usage:
    mlx-agent "how much disk is free?"                 # default 'general' persona
    mlx-agent --persona researcher "what to build next?"
    mlx-agent --list-personas
    mlx-agent --add-persona                            # interactive
    mlx-agent --edit-persona pentester
    mlx-agent --remove-persona pentester
    mlx-agent                                          # interactive REPL
    mlx-agent --yes "..."                              # auto-approve (careful)
"""
from __future__ import annotations
import argparse, json, os, re, shlex, subprocess, sys, textwrap, datetime, time, base64, shutil
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests not found — run with the workstation venv:\n"
             "  ~/.mlx-ai-workstation/.venv/bin/python mlx-agent.py \"...\"")

# ───────────────────────────── configuration ──────────────────────────────
GATEWAY   = os.environ.get("MLX_GATEWAY", "http://localhost:8000/v1")   # mlx-openai-server direct — LiteLLM drops tool calls
VISION_MODEL = os.environ.get("MLX_VISION_MODEL", "mlx-community/Qwen3-VL-8B-Instruct-4bit")
VISION_PORT  = os.environ.get("MLX_VISION_PORT", "8081")
VISION_SERVER = f"http://localhost:{VISION_PORT}"   # resident mlx_vlm.server (warm model)
SEARXNG   = os.environ.get("MLX_SEARXNG", "http://localhost:8888")
WORKDIR   = Path(os.environ.get("MLX_WORKDIR", str(Path.home() / ".mlx-ai-workstation")))
WORKSPACE = Path(os.environ.get("MLX_WORKSPACE", str(Path.home() / "MLX-AI")))
PERSONAS_FILE = WORKDIR / "personas.json"
DEFAULT_MODEL = os.environ.get("MLX_AGENT_MODEL", "coder:qwen3.6-27b")
MAX_STEPS = int(os.environ.get("MLX_AGENT_MAX_STEPS", "0"))               # 0 = unlimited tool steps
LOOP_GUARD = int(os.environ.get("MLX_AGENT_LOOP_GUARD", "10"))            # stop if the SAME tool call repeats N× in a row (0 = off); not a step cap, just anti-hang
MAX_SEARCHES = int(os.environ.get("MLX_AGENT_MAX_SEARCHES", "5"))         # per run, then steer to web_fetch/direct
MAX_SPAWN_DEPTH = int(os.environ.get("MLX_AGENT_MAX_SPAWN_DEPTH", "2"))   # subagents can nest this deep
MAX_SPAWNS = int(os.environ.get("MLX_AGENT_MAX_SPAWNS", "8"))             # total subagents per top-level run
SHELL_TIMEOUT = int(os.environ.get("MLX_AGENT_SHELL_TIMEOUT", "60"))
CODE_TIMEOUT = int(os.environ.get("MLX_AGENT_CODE_TIMEOUT", "120"))       # run_python wall-clock bound
MAX_TOKENS = int(os.environ.get("MLX_AGENT_MAX_TOKENS", "16384"))       # per model call; 0 = uncapped. A finite value stops a degenerate loop from running to the whole context.
TEMPERATURE = float(os.environ.get("MLX_AGENT_TEMPERATURE", "0.3"))
FREQ_PENALTY = float(os.environ.get("MLX_AGENT_FREQ_PENALTY", "0.4"))    # >0 discourages the "same phrase over and over" loops; 0 = don't send
PRESENCE_PENALTY = float(os.environ.get("MLX_AGENT_PRESENCE_PENALTY", "0.0"))

ALLOWED_ROOTS = [Path.home(), Path("/tmp"), WORKDIR, WORKSPACE, Path.cwd()]

SAFE_COMMANDS = {
    "df","du","ls","cat","head","tail","wc","grep","egrep","find","stat","file",
    "ps","top","uname","sw_vers","sysctl","uptime","whoami","id","date","which",
    "echo","env","printenv","vm_stat","system_profiler","ifconfig","ipconfig",
    "networksetup","hostname","sort","uniq","cut","awk","sed","tr","pwd","tree",
    "brew","git","pip","uv","python3","node","docker","launchctl","hf","ollama",
}
DANGEROUS = re.compile(r"(;|&&|\|\||>|<|`|\$\(|\brm\b|\bmv\b|\bdd\b|\bmkfs\b|\bsudo\b|\bkillall\b)")

# Autonomy: by default the agent RUNS everything (shell, code, file writes) without
# asking. Only two categories still require a human: installing packages, and deleting
# files/folders. Truly catastrophic commands are refused outright. Set
# MLX_AGENT_AUTONOMOUS=0 to go back to confirm-everything; a persona with
# approval:"strict" always asks regardless.
AUTONOMOUS = os.environ.get("MLX_AGENT_AUTONOMOUS", "1") != "0"

_INSTALL_RE = re.compile(
    r"\b(pip3?|pipx|uv|conda|mamba|poetry)\b[^\n]*\b(install|add|sync)\b"
    r"|\bbrew\b[^\n]*\b(install|reinstall|upgrade|tap)\b"
    r"|\b(npm|pnpm|yarn|bun)\b[^\n]*\b(i|install|add|ci)\b"
    r"|\b(cargo|gem|go)\b[^\n]*\binstall\b"
    r"|\b(apt|apt-get|port|dnf|yum)\b[^\n]*\binstall\b"
    r"|\bpip3?\s+install\b"
    r"|curl[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I)

_DELETE_RE = re.compile(
    r"\brm\b|\brmdir\b|\bunlink\b|\bshred\b|\btrash\b"
    r"|\bfind\b[^\n]*-delete\b|\bfind\b[^\n]*-exec\s+rm\b|\bgit\s+clean\b", re.I)

# Never run these, even with approval — they can wreck the machine.
_CATASTROPHIC_RE = re.compile(
    r"\bsudo\b|\bdd\b\s|\bmkfs\b|\bdiskutil\s+(erase|reformat|partition)\b"
    r"|:\s*\(\s*\)\s*\{|\bshutdown\b|\breboot\b|\bhalt\b"
    r"|\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+(/|~|\$HOME|/\*)(\s|$)"
    r"|>\s*/dev/(r?disk|sd)", re.I)

def _in_git_repo(path: str) -> bool:
    """True if `path` lives inside a git work tree (so changes/deletes are recoverable)."""
    try:
        p = os.path.abspath(os.path.expanduser(path))
        d = p if os.path.isdir(p) else (os.path.dirname(p) or ".")
        r = subprocess.run(["git", "-C", d, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False

def _delete_is_git_safe(command: str) -> bool:
    """True when EVERY path a delete command targets is inside a git work tree — then the
    deletion is version-controlled/recoverable, so it's safe to auto-approve without asking."""
    try: toks = shlex.split(command)
    except Exception: return False
    skip = {"rm","rmdir","unlink","shred","trash","find","git","clean","xargs","-exec",";","+","{}"}
    targets = [t for t in toks if not t.startswith("-") and t not in skip]
    if not targets:                       # e.g. `git clean -fd` acts on the cwd repo
        return _in_git_repo(os.getcwd())
    return all(_in_git_repo(t) for t in targets)

C = {"dim":"\033[2m","cyan":"\033[36m","yellow":"\033[33m","green":"\033[32m",
     "red":"\033[31m","bold":"\033[1m","magenta":"\033[35m","reset":"\033[0m"}
def c(s, col): return f"{C[col]}{s}{C['reset']}" if sys.stdout.isatty() else s

AUTO_YES = False
RUN_APPROVE_ALL = False   # per-run "allow the rest of this run"; reset at each top-level run
STRICT_RUN = False        # set from the active persona; disables the 'allow all' escape hatch

# Pluggable I/O hooks. The CLI leaves these None (terminal input + stdout). The
# Telegram bridge sets them to route approvals to inline buttons, multi-choice to
# button rows, and progress to a single live-updating message.
ASK_FN = None      # ASK_FN(prompt:str, allow_all:bool) -> 'y' | 'n' | 'a'
CHOICE_FN = None   # CHOICE_FN(question:str, options:list) -> chosen str
EMIT_FN = None     # EMIT_FN(line:str) -> None   (a progress/trace line)
STREAM_FN = None   # STREAM_FN(piece:str) -> None (a token/delta of the streaming answer)
_ANSI = re.compile(r"\033\[[0-9;]*m")

def emit(line: str):
    print(line)
    if EMIT_FN is not None:
        try: EMIT_FN(_ANSI.sub("", line))
        except Exception: pass

def _ask_yn(prompt: str, can_all: bool) -> str:
    if ASK_FN is not None:
        try: return ASK_FN(prompt, can_all)
        except Exception: return "n"
    opts = "[y]es · [n]o · [a]llow rest of this run" if can_all else "[y]es · [n]o"
    try: ans = input(c(f"  ⚠ {prompt}  {opts}: ", "yellow")).strip().lower()
    except (EOFError, KeyboardInterrupt): return "n"
    if ans in ("y","yes"): return "y"
    if ans in ("a","all") and can_all: return "a"
    return "n"

def confirm(prompt: str, force_ask: bool = False, kind: str = "general") -> bool:
    """Approve an action. In AUTONOMOUS mode the agent runs freely; only kind in
    {install, delete} still asks a human. A strict persona (STRICT_RUN) always asks."""
    global RUN_APPROVE_ALL
    gated = kind in ("install", "delete")
    if AUTO_YES:
        emit(c(f"  (auto-approved) {prompt}", "dim")); return True
    if AUTONOMOUS and not STRICT_RUN and not force_ask and not gated:
        return True                                   # silent auto-run for normal task actions
    if RUN_APPROVE_ALL and not STRICT_RUN and not force_ask and not gated:
        emit(c(f"  (approved for this run) {prompt}", "dim")); return True
    can_all = not (STRICT_RUN or force_ask or gated)  # no 'allow-all' for installs/deletes — each asks
    ans = _ask_yn(prompt, can_all)
    if ans == "y": return True
    if ans == "a" and can_all:
        RUN_APPROVE_ALL = True; return True
    return False

# ─────────────────────────── persona registry ─────────────────────────────
BASE_SYSTEM = (
    "You are a local AI assistant running on the user's Mac with REAL tools. When a task "
    "needs live system data, files, code execution, or current web info, CALL A TOOL rather "
    "than guessing or claiming you lack access. Use the fewest tool calls that answer the task. "
    "After tools return, answer concisely from their ACTUAL output. Never fabricate tool results.\n"
    "AUTONOMY: you are cleared to act. Run shell commands, execute code, and read/write/overwrite "
    "files freely to complete the task — do NOT ask the user for permission for ordinary steps, and "
    "do NOT stop to narrate what you're about to do; just call the tool and do it. The ONLY actions "
    "that need the user's approval are installing packages and deleting files or folders; the system "
    "handles asking for those, so proceed normally and let it prompt when needed. Inside a git "
    "repository, deletions are auto-approved (git can recover them), so you can refactor freely there.\n"
    "INTERNET: you DO have live web access via web_search and web_fetch. For anything current or "
    "factual you're unsure of — news, sports scores, prices, release dates, docs — search first. "
    "NEVER tell the user you can't access the internet or browse the web; you can, so do it.\n"
    "CODE: for anything without a dedicated tool, WRITE AND RUN a short Python script with run_python "
    "(the runtime has mlx, requests, psutil, etc.) instead of giving up or stringing together many shell "
    "calls. print() the result so you get it back. This is your general-purpose way to actually do work.\n"
    "SWIFT/XCODE: to start ANY Swift, macOS, or iOS app, call scaffold_swift first (it uses SwiftPM and "
    "produces a buildable Package.swift that opens in Xcode). NEVER hand-write .xcodeproj or "
    "project.pbxproj — they will not work. Then write source under Sources/ with write_file, and run "
    "`swift build` via run_shell to compile and fix errors iteratively before moving on.\n"
    "SELF-HEALING: if a task needs a tool/package/library/CLI you don't have, DO NOT give up or "
    "say you lack the capability. Instead: (1) use web_search to find what to install and the exact "
    "commands, then (2) call propose_capability with a concrete plan. The user will review and approve. "
    "After it installs and verifies, retry the original task. Prefer package installs (pip/brew/uv/npm); "
    "only propose fetching a web script when no package exists.\n"
    "DELEGATION: when a task spans specialties, you may call spawn_subagent(persona, task) to hand a "
    "sub-task to a specialist (personas include researcher, dev, qa, ba, reasoner, general). Do simple "
    "steps yourself; delegate the ones a specialist fits, then synthesize the results.\n"
    "FILES: when the user asks you to CREATE, WRITE, SAVE, PRODUCE, GENERATE, or UPDATE a file "
    "(a README, script, config, document, etc.), you MUST call write_file with the COMPLETE final "
    "content and the target path — do NOT print the file's contents as your chat answer and stop. "
    "Writing the file IS the deliverable. To study or summarize an existing file first, call read_file "
    "to get its real content (never guess it). After writing, your answer is just a short confirmation "
    "of what you wrote and the path — not the whole file again. If the user gives a path, use it exactly; "
    "if not, write under the shared workspace ~/MLX-AI (e.g. ~/MLX-AI/<short-name>). Never invent absolute "
    "paths for directories you haven't confirmed exist — check with list_dir or pwd first if unsure.\n"
    "SEARCH DISCIPLINE: web_search is for finding a URL or a quick fact. Do NOT search repeatedly for the "
    "same thing. Once a search returns a promising link, use web_fetch to READ it. To learn a library's "
    "API, the fastest route is usually propose_capability to install it, then introspect (python -c "
    "\"import x; help(x)\") — not more searching. After ~3 searches on one question, switch tactics.")

DEFAULT_PERSONAS = {
    "orchestrator": {
        "description": "Main agent — plans a goal, delegates to specialist subagents, synthesizes.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": ("You are the orchestrator, the user's main agent. Plan the goal as concrete steps. "
                          "Delegate specialist steps with spawn_subagent(persona, task) — and put the CONCRETE "
                          "context the specialist needs directly in the task string (e.g. don't tell dev to 'write "
                          "hello-world for the popular framework' — tell it exactly which framework and any facts you "
                          "already found, so it doesn't re-research). Personas: researcher (web/trends), dev "
                          "(build/run code), qa (test), ba (specs), reasoner (hard analysis). Do trivial steps "
                          "yourself. ALWAYS finish with a short synthesized answer to the user that states the key "
                          "findings and what each subagent produced — never end on a raw tool result. Do NOT spawn "
                          "the same task twice: if a subagent already attempted a step, use its result or refine it "
                          "yourself rather than re-delegating the identical task. "
                          "If the goal is to PRODUCE A FILE (a README, script, doc, etc.), the deliverable is "
                          "the file WRITTEN TO DISK: call write_file yourself with the complete content (or have "
                          "a subagent do it), then finish with a one-line confirmation of the path — do NOT paste "
                          "the file's full contents as your answer and stop, and do NOT end on a raw tool result."),
    },
    "general": {
        "description": "General-purpose daily assistant.",
        "model": DEFAULT_MODEL,
        "allowed_tools": "all",
        "system_prompt": "You handle any everyday task: questions, code, files, system checks, web lookups.",
    },
    "ba": {
        "description": "Business Analyst — turns goals into clear requirements.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": ("You are a Business Analyst. Clarify the user's goal, break it into concrete "
                          "requirements and acceptance criteria, and write a concise spec. Ask a blocking "
                          "question only when genuinely necessary."),
    },
    "dev": {
        "description": "Software Developer — implements and runs code.",
        "model": "coder:qwen3.6-27b",
        "allowed_tools": "all",
        "system_prompt": ("You are a Software Developer. Implement the requested code, write it to files, "
                          "then run and debug it with the shell. Keep changes minimal and working; show what you ran."),
    },
    "qa": {
        "description": "QA Engineer — reviews and tests.",
        "model": "qa:qwen3.6-27b",
        "allowed_tools": "all",
        "system_prompt": ("You are a QA Engineer. Review code for correctness and edge cases, write and RUN "
                          "tests, and report pass/fail with specifics and reproduction steps."),
    },
    "researcher": {
        "description": "Research Analyst — trend/market research from the web.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": ("You are a Research Analyst. Use web_search to gather current, credible information, "
                          "cross-check multiple sources, and summarize findings with links and a clear, actionable "
                          "recommendation. Distinguish facts from your inference."),
    },
    "reasoner": {
        "description": "Deep reasoning / math specialist (DeepSeek-R1-32B). Best for hard analysis, not tool-heavy work.",
        "model": "reasoner:deepseek-r1-32b",
        "allowed_tools": "all",
        "system_prompt": ("You are a careful reasoning specialist. Think step by step through hard problems in "
                          "math, logic, analysis, and debugging. Show the key steps of your reasoning, then give a "
                          "clear final answer. Use tools when a fact must be checked rather than assumed."),
    },
}

# Gateway aliases that no longer exist (renamed/removed over the project). A STARTER
# persona still pointing at one of these is auto-reset to its current DEFAULT_PERSONAS
# model on load. User-created personas are never touched.
RETIRED_MODELS = {
    "coder", "orchestrator", "reasoner", "qa", "vision", "embed",   # pre-colon short names
    "reasoner:qwen3.6-27b", "vision:qwen3.6-27b",                    # renamed colon aliases
}

def load_personas() -> dict:
    if not PERSONAS_FILE.exists():
        WORKDIR.mkdir(parents=True, exist_ok=True)
        PERSONAS_FILE.write_text(json.dumps(DEFAULT_PERSONAS, indent=2))
        return dict(DEFAULT_PERSONAS)
    try:
        p = json.loads(PERSONAS_FILE.read_text())
    except Exception as e:
        print(c(f"warning: personas.json unreadable ({e}); using defaults", "yellow"))
        return dict(DEFAULT_PERSONAS)
    changed = False
    # 1) add any missing STARTER personas (e.g. a newly-shipped 'orchestrator')
    for name, cfg in DEFAULT_PERSONAS.items():
        if name not in p:
            p[name] = dict(cfg); changed = True
    # 2) repair dead model aliases on STARTERS only → reset to their current intended
    #    model. User-created personas are left exactly as the user set them.
    for name in DEFAULT_PERSONAS:
        if p.get(name, {}).get("model") in RETIRED_MODELS:
            p[name]["model"] = DEFAULT_PERSONAS[name]["model"]; changed = True
    if changed:
        try: save_personas(p)
        except Exception: pass
    return p

def save_personas(p: dict):
    WORKDIR.mkdir(parents=True, exist_ok=True)
    PERSONAS_FILE.write_text(json.dumps(p, indent=2))

def installed_models() -> list:
    try:
        r = requests.get(f"{GATEWAY}/models", timeout=8)
        return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return []

# ─────────────────────────────── tools ────────────────────────────────────
def _within_allowed(p: Path) -> bool:
    try: rp = p.expanduser().resolve()
    except Exception: return False
    return any(str(rp).startswith(str(r.resolve())) for r in ALLOWED_ROOTS)

# Vision. Preferred path: a resident mlx_vlm.server (model stays warm → fast, and it
# sidesteps mlx-openai-server's multimodal hang). If that server isn't up, fall back
# to a bounded one-shot subprocess that loads the model fresh — slower, but always works.
_VISION_CODE = (
    "import sys\n"
    "from mlx_vlm import load, generate\n"
    "from mlx_vlm.prompt_utils import apply_chat_template\n"
    "from mlx_vlm.utils import load_config\n"
    "repo, img, q = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "model, proc = load(repo)\n"
    "cfg = load_config(repo)\n"
    "fp = apply_chat_template(proc, cfg, q, num_images=1)\n"
    "out = generate(model, proc, fp, [img], verbose=False, max_tokens=512)\n"
    "print((getattr(out, 'text', None) or str(out)).strip())\n"
)

def _image_to_url(src: str) -> str:
    """A remote URL is passed through; a local file becomes a base64 data URL."""
    if src.startswith("http://") or src.startswith("https://"):
        return src
    data = Path(src).read_bytes()
    ext = Path(src).suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(ext, "png")
    return f"data:image/{mime};base64," + base64.b64encode(data).decode()

def _vision_via_server(src: str, question: str, timeout: int = 300) -> str:
    """Ask the resident mlx_vlm.server (OpenAI-compatible). Raises on any failure so
    the caller can fall back to the subprocess."""
    body = {"model": VISION_MODEL, "max_tokens": 512, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": _image_to_url(src)}}]}]}
    r = requests.post(f"{VISION_SERVER}/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()

def t_see_image(image_path: str, question: str = "Describe this image in detail.") -> str:
    src = (image_path or "").strip()
    if not (src.startswith("http://") or src.startswith("https://")):
        p = Path(src)
        if not _within_allowed(p): return f"[refused: {src} is outside the allowed folders]"
        if not p.expanduser().exists(): return f"[no such image: {src}]"
        src = str(p.expanduser().resolve())
    emit(c(f"  👁  see_image({src[:60]} · {VISION_MODEL})", "green"))
    try:                                   # fast path: warm resident server
        out = _vision_via_server(src, question)
        if out: return out
    except Exception:
        pass                               # server down/erroring → fall back below
    try:                                   # reliable path: fresh bounded subprocess
        r = subprocess.run([sys.executable, "-c", _VISION_CODE, VISION_MODEL, src, question],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "[vision timed out (600s) — the model may be too large or the multimodal path is hanging]"
    if r.returncode != 0:
        return f"[vision failed: {(r.stderr or '').strip()[-400:]}]"
    return (r.stdout or "").strip() or "[vision produced no output]"

def t_run_shell(command: str) -> str:
    command = command.strip()
    # 1) Refuse machine-wrecking commands outright — no approval path.
    if _CATASTROPHIC_RE.search(command):
        return ("[blocked: refusing a destructive/system-level command (sudo, disk wipe, "
                "rm -rf / , shutdown, etc.). If this is truly intended, run it yourself.]")
    # 2) Installs and deletes need a human, even in autonomous mode.
    if _INSTALL_RE.search(command):
        if not confirm(f"install packages: {c(command,'bold')}", kind="install"):
            return "[install denied by user]"
    elif _DELETE_RE.search(command):
        # Deletes inside a git work tree are recoverable → auto-approve (unless a strict persona).
        git_safe = AUTONOMOUS and not STRICT_RUN and _delete_is_git_safe(command)
        if git_safe:
            emit(c("  (git-tracked → recoverable → auto-approved delete)", "dim"))
        elif not confirm(f"DELETE files/folders: {c(command,'bold')}", kind="delete"):
            return "[delete denied by user]"
    elif STRICT_RUN:
        # A strict persona confirms every command, autonomy notwithstanding.
        if not confirm(f"run shell: {c(command,'bold')}", kind="run"):
            return "[denied by user]"
    elif not AUTONOMOUS:
        # Legacy confirm-the-risky behaviour when autonomy is switched off.
        segments = [s.strip() for s in command.split("|")]
        first_tokens = []
        for seg in segments:
            try: first_tokens.append(shlex.split(seg)[0])
            except Exception: first_tokens.append("")
        all_safe = bool(first_tokens) and all(t in SAFE_COMMANDS for t in first_tokens)
        if not (all_safe and not DANGEROUS.search(command)):
            if not confirm(f"run shell: {c(command,'bold')}", kind="run"):
                return "[denied by user]"
    # 3) Everything else runs freely.
    emit(c(f"  $ {command}", "cyan"))
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=SHELL_TIMEOUT)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        out = out.strip() or "[no output]"
        return out[:6000] + ("\n…[truncated]" if len(out) > 6000 else "")
    except subprocess.TimeoutExpired: return f"[timed out after {SHELL_TIMEOUT}s]"
    except Exception as e: return f"[error: {e}]"

def t_scaffold_swift(directory: str, name: str = "App", kind: str = "executable") -> str:
    """Create a buildable Swift package with SwiftPM (`swift package init`). This is the RELIABLE
    way to start a Swift/Xcode project: it produces a Package.swift that `swift build` compiles and
    that opens directly in Xcode — no fragile .xcodeproj/project.pbxproj to hand-write."""
    d = Path(directory).expanduser()
    if not _within_allowed(d):
        return f"[refused: {directory} is outside allowed paths]"
    kinds = {"executable", "library", "tool", "empty", "macro", "build-tool-plugin", "command-plugin"}
    k = kind if kind in kinds else "executable"
    if shutil.which("swift") is None:
        return "[swift not found — install Xcode Command Line Tools: `xcode-select --install`, then retry]"
    if not confirm(f"scaffold Swift {k} package {c(name,'bold')} in {c(str(d),'bold')} (swift package init)?", kind="run"):
        return "[denied by user]"
    emit(c(f"  🛠  scaffold_swift({name} · {k} · {d})", "cyan"))
    try:
        d.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["swift", "package", "init", "--type", k, "--name", name],
                           cwd=str(d), capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return f"[scaffold failed: {((r.stderr or r.stdout) or '').strip()[-400:]}]"
        tree = subprocess.run(["find", str(d), "-maxdepth", "2", "-not", "-path", "*/.*"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
        return (f"[scaffolded Swift {k} package '{name}' in {d}]\n{tree}\n"
                f"Build: (cd {d} && swift build)   ·   Open in Xcode: open {d}/Package.swift\n"
                f"Now write the Swift source under {d}/Sources/ with write_file, then run `swift build` "
                f"via run_shell and fix any errors before moving on.")
    except subprocess.TimeoutExpired:
        return "[scaffold timed out]"
    except Exception as e:
        return f"[error: {e}]"

def t_disk_usage(_: str = "") -> str: return t_run_shell("df -h /")

def t_run_python(code: str, timeout: int = 0) -> str:
    """Write an ephemeral Python script and run it in the workstation's Python (which has
    mlx, requests, psutil, etc.). This is the general-purpose 'do it in code' escape hatch —
    for anything without a dedicated tool. print() whatever you want returned."""
    code = (code or "").strip()
    if not code:
        return "[no code provided]"
    to = int(timeout) if (timeout and int(timeout) > 0) else CODE_TIMEOUT
    preview = code if len(code) <= 400 else code[:400] + " …"
    if not confirm(f"run python script ({len(code)} chars, {to}s):\n{c(preview,'dim')}", kind="run"):
        return "[denied by user]"
    emit(c(f"  🐍 run_python ({len(code)} chars)", "cyan"))
    import tempfile
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".py", prefix="agent_", dir="/tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(code)
        r = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=to)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        if r.returncode != 0 and not out.strip():
            out = f"[exited {r.returncode} with no output]"
        out = out.strip() or "[ran OK, no output — print() what you want back]"
        return out[:6000] + ("\n…[truncated]" if len(out) > 6000 else "")
    except subprocess.TimeoutExpired:
        return f"[timed out after {to}s]"
    except Exception as e:
        return f"[error: {e}]"
    finally:
        if path:
            try: os.remove(path)
            except Exception: pass

def t_read_file(path: str) -> str:
    p = Path(path).expanduser()
    if not _within_allowed(p): return f"[refused: {path} is outside allowed paths]"
    if not p.is_file(): return f"[not a file: {path}]"
    try:
        data = p.read_text(errors="replace")
        return data[:8000] + ("\n…[truncated]" if len(data) > 8000 else "")
    except Exception as e: return f"[error reading {path}: {e}]"

def t_list_dir(path: str = ".") -> str:
    p = Path(path).expanduser()
    if not _within_allowed(p): return f"[refused: {path} is outside allowed paths]"
    if not p.is_dir(): return f"[not a directory: {path}]"
    try:
        items = sorted(p.iterdir())
        return "\n".join(("📁 " if i.is_dir() else "📄 ") + i.name for i in items[:200]) or "[empty]"
    except Exception as e: return f"[error: {e}]"

def t_write_file(path: str, content: str, mode: str = "w") -> str:
    p = Path(path).expanduser()
    if not _within_allowed(p): return f"[refused: {path} is outside allowed paths]"
    append = str(mode).lower() in ("a", "append")
    verb = "append to" if append else "write"
    preview = content if len(content) < 300 else content[:300] + "…"
    if not confirm(f"{verb} {len(content)} chars {'onto' if append else 'to'} {c(str(p),'bold')}?\n{c(preview,'dim')}\n", kind="write"):
        return "[write denied by user]"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w") as fh:
            fh.write(content)
        total = p.stat().st_size
        return f"[{'appended' if append else 'wrote'} {len(content)} chars to {p} (file now {total} bytes)]"
    except Exception as e: return f"[error writing {path}: {e}]"

def t_web_search(query: str) -> str:
    try:
        r = requests.get(f"{SEARXNG}/search", params={"q": query, "format": "json"}, timeout=20)
        results = r.json().get("results", [])[:5]
        if not results: return "[no results]"
        return "\n\n".join(f"{x.get('title','')}\n{x.get('url','')}\n{x.get('content','')}" for x in results)
    except Exception as e: return f"[search error: {e} — is SearXNG up on {SEARXNG}?]"

def t_web_fetch(url: str) -> str:
    """Fetch and return the readable text of a web page — read a source/doc instead of re-searching."""
    if not re.match(r"^https?://", url or ""):
        return "[error: url must start with http:// or https://]"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "mlx-agent/1.0"})
        ct = r.headers.get("content-type", "")
        text = r.text
        if "html" in ct:   # strip tags/script/style to readable text
            text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"&[a-z]+;", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text[:10000] + ("\n…[truncated]" if len(text) > 10000 else "")
    except Exception as e:
        return f"[fetch error for {url}: {e}]"

def t_ask_choice(question: str, options=None) -> str:
    """Ask the user to pick one of several options. Renders as buttons on Telegram."""
    if isinstance(options, str): options = [options]
    options = [str(o) for o in (options or []) if str(o).strip()]
    if not options: return "[error: ask_choice needs a non-empty 'options' list]"
    if CHOICE_FN is not None:
        try: return CHOICE_FN(question, options)
        except Exception: return options[0]
    emit(c(f"  ? {question}", "yellow"))
    for i, o in enumerate(options, 1): emit(f"    {i}) {o}")
    try: raw = input(c("  choose [number]: ", "cyan")).strip()
    except (EOFError, KeyboardInterrupt): return options[0]
    if raw.isdigit() and 1 <= int(raw) <= len(options): return options[int(raw)-1]
    return raw or options[0]

# ── self-healing: acquire missing capabilities (with approval + audit) ──
CAP_LOG = WORKSPACE / "capability-log.jsonl"
VENV_PY = str(WORKDIR / ".venv" / "bin" / "python")   # the workstation venv interpreter

def _venv_scope(cmd: str) -> str:
    """Rewrite a package-install command to target the workstation venv, never system Python."""
    c = cmd.strip()
    # pip / pip3 / python -m pip / python3 -m pip  →  <venv python> -m pip
    c = re.sub(r"^(sudo\s+)?(python3?\s+-m\s+)?pip3?\s+install\b",
               f'"{VENV_PY}" -m pip install', c)
    # uv pip install ...  →  uv pip install --python <venv python> ...
    if re.match(r"^uv\s+pip\s+install\b", c) and "--python" not in c:
        c = c.replace("uv pip install", f'uv pip install --python "{VENV_PY}"', 1)
    return c

def _log_capability(entry: dict):
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        with open(CAP_LOG, "a") as f: f.write(json.dumps(entry) + "\n")
    except Exception: pass

def _run_install_cmd(cmd: str) -> tuple:
    print(c(f"  $ {cmd}", "cyan"))
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=900)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired: return 124, "[timed out after 900s]"
    except Exception as e: return 1, f"[error: {e}]"

def t_propose_capability(need: str, kind: str = "package", commands=None,
                         script_url: str = "", run_command: str = "",
                         verify: str = "", reason: str = "") -> str:
    kind = (kind or "package").lower()
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(c("\n  ┌─ capability request ───────────────────────────────", "magenta"))
    print(c(f"  │ need   : {need}", "magenta"))
    if reason: print(c(f"  │ reason : {reason}", "magenta"))

    if kind == "script":
        if not script_url: return "[error: script kind requires script_url]"
        try: body = requests.get(script_url, timeout=30).text
        except Exception as e: return f"[could not fetch {script_url}: {e}]"
        print(c(f"  │ source : {script_url}", "magenta"))
        print(c(f"  │ run    : {run_command or '(bash the file)'}", "magenta"))
        print(c("  └────────────────────────────────────────────────────", "magenta"))
        print(c("  ── SCRIPT CONTENT — review carefully, this runs on YOUR machine ──", "yellow"))
        print(body[:8000] + ("\n…[truncated — full script saved before run]" if len(body) > 8000 else ""))
        print(c("  ──────────────────────────────────────────────────────────────", "yellow"))
        # force_ask: arbitrary web scripts ALWAYS ask, even under "allow rest of run"
        approved = confirm(f"fetch-and-run this script from {script_url}?", force_ask=True)
        _log_capability({"ts":ts,"kind":"script","need":need,"url":script_url,"run":run_command,"approved":approved})
        if not approved: return "[user declined the script — do not retry it]"
        dest = Path("/tmp") / f"cap_{int(datetime.datetime.now().timestamp())}.sh"
        dest.write_text(body)
        rc, out = _run_install_cmd(run_command or f"bash {dest}")
        return f"[script {'ok' if rc==0 else 'FAILED rc='+str(rc)}]\n{out[:3000]}"

    # package install
    if isinstance(commands, str): commands = [commands]
    commands = commands or []
    if not commands: return "[error: package kind requires commands]"
    print(c("  │ plan   :", "magenta"))
    for cmd in commands: print(c(f"  │   $ {cmd}", "bold"))
    print(c("  └────────────────────────────────────────────────────", "magenta"))
    approved = confirm("install plan (into workstation venv):\n  " + "\n  ".join(commands), kind="install")
    _log_capability({"ts":ts,"kind":"package","need":need,"commands":commands,"approved":approved})
    if not approved: return "[user declined the install — do not retry it]"
    outs = []
    for cmd in commands:
        scoped = _venv_scope(cmd)
        rc, out = _run_install_cmd(scoped); outs.append(f"$ {scoped}\n{out[:1500]}")
        if rc != 0: outs.append(f"[stopped: '{scoped}' failed rc={rc}]"); break
    if verify:
        rc, vout = _run_install_cmd(verify)
        outs.append(f"[verify {'OK — capability ready' if rc==0 else 'FAILED (installed ≠ working)'}] {vout[:500]}")
    return "\n".join(outs)

TOOLS = {
    "run_shell":  (t_run_shell,  "Run a shell command on the host and return its output. Read-only commands "
                                 "run automatically; others ask the user first."),
    "disk_usage": (t_disk_usage, "Return human-readable free/used disk space for the root volume."),
    "run_python": (t_run_python, "Write and run an ephemeral Python script in the workstation's Python "
                   "(which has mlx, requests, psutil, etc.). This is your general-purpose way to DO a task "
                   "that has no dedicated tool — parse/transform data, call a library, do multi-step logic, "
                   "or build file content programmatically. print() whatever you want back (stdout is returned). "
                   "Prefer one short script over many shell calls or giving up. Args: code (str), timeout (int, "
                   "optional seconds). Asks the user to confirm first."),
    "read_file":  (t_read_file,  "Read a text file from an allowed path."),
    "list_dir":   (t_list_dir,   "List the contents of a directory in an allowed path."),
    "write_file": (t_write_file, "Create or overwrite a file on disk with the given content. "
                   "Call this WHENEVER the user asks you to create, write, save, produce, generate, "
                   "or update a file (README, script, config, doc). Pass the COMPLETE final content — "
                   "do not just print it in chat. For a file too large for one call, write the first "
                   "part then call again with mode='a' to append more. Asks the user to confirm first."),
    "scaffold_swift": (t_scaffold_swift, "Start a buildable Swift/Xcode project the RELIABLE way, using "
                   "SwiftPM (swift package init). Produces a Package.swift that `swift build` compiles and "
                   "that opens in Xcode. Use this to begin ANY Swift/macOS/iOS app — NEVER hand-write "
                   ".xcodeproj or project.pbxproj. Args: directory, name, kind (executable|library|empty). "
                   "After scaffolding, write source files under Sources/ and build with run_shell."),
    "web_search": (t_web_search, "Search the web via the local private SearXNG for current info."),
    "web_fetch":  (t_web_fetch,  "Fetch and read the actual text of a web page by URL. Use this to READ a "
                                 "doc, README, or source file instead of repeatedly searching — e.g. after a "
                                 "search returns a promising URL, fetch it to get the real content."),
    "see_image":  (t_see_image,  "Look at an image and answer a question about it (OCR, description, charts, "
                                 "screenshots). Args: image_path (a local path or http(s) URL) and question. "
                                 "Runs a local vision model — first use loads it, so allow some time."),
    "ask_choice": (t_ask_choice, "Ask the user to choose among concrete options (renders as tappable buttons on "
                                 "Telegram). Use when you genuinely need the user to decide a direction. Args: "
                                 "question (str) and options (list of short strings). Returns the chosen option."),
    "propose_capability": (t_propose_capability,
        "Acquire a MISSING capability instead of giving up. Call this when the task needs a tool, "
        "package, library, or CLI you don't have. Provide: need (what's missing), kind ('package' or "
        "'script'), and for packages a 'commands' list (e.g. ['uv pip install pytesseract','brew install "
        "tesseract']) plus an optional 'verify' command; for scripts a 'script_url' and 'run_command'. "
        "The user reviews and approves before anything runs. After it succeeds, retry the original task."),
    "spawn_subagent": (None,
        "Delegate a sub-task to a specialist persona. Args: persona (registry name like researcher, dev, qa, "
        "ba, reasoner, general) and task (what that specialist should do). Returns the subagent's final answer "
        "plus a short trace of the tools it used. Use this to break a big goal into specialist steps."),
}
def schema(name, props, req):
    return {"type":"function","function":{"name":name,"description":TOOLS[name][1],
            "parameters":{"type":"object","properties":props,"required":req}}}
ALL_SCHEMAS = {
    "run_shell":  schema("run_shell", {"command":{"type":"string"}}, ["command"]),
    "disk_usage": schema("disk_usage", {}, []),
    "run_python": schema("run_python", {"code":{"type":"string","description":"the Python source to run"},
                                        "timeout":{"type":"integer","description":"optional wall-clock seconds"}}, ["code"]),
    "read_file":  schema("read_file", {"path":{"type":"string"}}, ["path"]),
    "list_dir":   schema("list_dir", {"path":{"type":"string"}}, []),
    "write_file": schema("write_file", {"path":{"type":"string"},"content":{"type":"string"},
                                         "mode":{"type":"string","enum":["w","a"],"description":"w=overwrite (default), a=append"}}, ["path","content"]),
    "scaffold_swift": schema("scaffold_swift", {"directory":{"type":"string","description":"folder to create the package in"},
                                                "name":{"type":"string","description":"package/app name"},
                                                "kind":{"type":"string","enum":["executable","library","empty"]}}, ["directory"]),
    "web_search": schema("web_search", {"query":{"type":"string"}}, ["query"]),
    "web_fetch":  schema("web_fetch", {"url":{"type":"string"}}, ["url"]),
    "see_image":  schema("see_image", {"image_path":{"type":"string","description":"local path or http(s) URL of the image"},
                                       "question":{"type":"string","description":"what to ask about the image"}}, ["image_path"]),
    "ask_choice": schema("ask_choice", {
        "question":{"type":"string"},
        "options":{"type":"array","items":{"type":"string"}},
    }, ["question","options"]),
    "propose_capability": schema("propose_capability", {
        "need":{"type":"string","description":"what capability is missing"},
        "kind":{"type":"string","enum":["package","script"]},
        "commands":{"type":"array","items":{"type":"string"},"description":"install commands for kind=package"},
        "script_url":{"type":"string","description":"URL to fetch for kind=script"},
        "run_command":{"type":"string","description":"how to run the fetched script"},
        "verify":{"type":"string","description":"command to confirm the capability now works"},
        "reason":{"type":"string","description":"why it's needed for the task"},
    }, ["need","kind"]),
    "spawn_subagent": schema("spawn_subagent", {
        "persona":{"type":"string","description":"registry persona to delegate to (researcher, dev, qa, ba, reasoner, general)"},
        "task":{"type":"string","description":"the sub-task for that specialist to perform"},
    }, ["persona","task"]),
}
def tools_for(persona: dict) -> tuple:
    allowed = persona.get("allowed_tools", "all")
    if allowed == "all" or not allowed:
        names = list(ALL_SCHEMAS.keys())
    else:
        names = [t for t in allowed if t in ALL_SCHEMAS]
    return set(names), [ALL_SCHEMAS[n] for n in names]

# ─────────────────────────── model plumbing ───────────────────────────────
def _looks_degenerate(text: str) -> bool:
    """True when a generation fell into a repetition loop: many lines, almost none unique."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return len(lines) >= 15 and len(set(lines)) <= 5

def strip_thinking(text: str) -> str:
    if not text: return ""
    if "</think>" in text: text = text.split("</think>")[-1]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()

_SERVED = {"names": None, "t": 0.0}
def _served_models():
    """Cache the server's real model IDs (served_model_name) for ~60s."""
    now = time.time()
    if _SERVED["names"] is None or now - _SERVED["t"] > 60:
        try:
            r = requests.get(f"{GATEWAY}/models", timeout=8)
            _SERVED["names"] = [d.get("id") for d in (r.json().get("data") or []) if d.get("id")]
        except Exception:
            _SERVED["names"] = _SERVED["names"] or []
        _SERVED["t"] = now
    return _SERVED["names"]

def _resolve_model(m: str) -> str:
    """Map a persona/LiteLLM-style alias to the server's actual served_model_name.
    e.g. 'orchestrator:qwen3.6-35b' -> 'qwen36-35b'. Bypassing LiteLLM means the mlx
    server only knows its served names, so we translate before every call."""
    m = (m or "").strip()
    served = _served_models()
    if served and m in served:
        return m
    cand = m.split(":")[-1].replace(".", "")        # 'coder:qwen3.6-27b' -> 'qwen36-27b'
    if not served or cand in served:
        return cand
    for s in served:                                 # last resort: loose match
        if cand and (cand in s or s in cand):
            return s
    return cand

def call_gateway(model: str, messages: list, schemas: list) -> dict:
    # Always non-streaming. Reliable tool-call detection is the whole point of the
    # agent, and streaming tool-calls through the local MLX backend is unreliable —
    # the model's tool call can arrive as plain text and get mistaken for the answer.
    # The final answer is streamed to live front-ends separately (see run_agent), by
    # replaying it once generation is safely complete.
    body = {"model": _resolve_model(model), "messages": messages, "tools": schemas,
            "tool_choice": "auto", "temperature": TEMPERATURE, "stream": False}
    if MAX_TOKENS > 0:                       # 0 → uncapped; a finite value bounds a runaway loop
        body["max_tokens"] = MAX_TOKENS
    if FREQ_PENALTY:                         # curbs degenerate "I will run the script" repetition loops
        body["frequency_penalty"] = FREQ_PENALTY
    if PRESENCE_PENALTY:
        body["presence_penalty"] = PRESENCE_PENALTY
    r = requests.post(f"{GATEWAY}/chat/completions", json=body, timeout=600)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]

def execute_tool_call(tc: dict, allowed_names: set) -> str:
    name = tc["function"]["name"]
    if name not in allowed_names:
        return f"[refused: this persona is not permitted to use '{name}']"
    try: args = json.loads(tc["function"].get("arguments") or "{}")
    except json.JSONDecodeError: args = {}
    fn = TOOLS.get(name, (None,))[0]
    if fn is None: return f"[unknown tool: {name}]"
    emit(c(f"  → {name}({', '.join(f'{k}={v!r}' for k,v in args.items())})", "green"))
    try: return fn(**args)
    except TypeError as e: return f"[bad arguments for {name}: {e}]"
    except Exception as e: return f"[tool error in {name}: {e}]"

# ── activity beacon: a running top-level task drops a small json file so the
#    dashboard can show "what's an agent doing right now" across processes ─────
ACTIVITY_DIR = WORKDIR / "activity"
class _Activity:
    def __init__(self, query: str, persona: dict, depth: int):
        self.path = None
        if depth != 0:                      # only the top-level task is tracked (subagents show as 'current')
            return
        try:
            ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
            self.path = ACTIVITY_DIR / f"{os.getpid()}.json"
            self.rec = {"pid": os.getpid(), "task": (query or "").strip()[:240],
                        "model": persona.get("model", ""),
                        "persona": (persona.get("description", "") or "")[:48],
                        "started": time.time(), "last": time.time(), "current": "starting…"}
            self._flush()
        except Exception:
            self.path = None
    def update(self, current: str):
        if not self.path: return
        try:
            self.rec["current"] = (current or "")[:140]; self.rec["last"] = time.time(); self._flush()
        except Exception: pass
    def _flush(self):
        self.path.write_text(json.dumps(self.rec))
    def done(self):
        if not self.path: return
        try: self.path.unlink()
        except Exception: pass

def run_agent(query: str, persona: dict, depth: int = 0, spawn_ctx: dict = None,
              trace_out: list = None, history: list = None) -> str:
    global RUN_APPROVE_ALL, STRICT_RUN
    if spawn_ctx is None: spawn_ctx = {"count": 0}
    if depth == 0:
        RUN_APPROVE_ALL = False                       # each new top-level task starts fresh
    prev_strict = STRICT_RUN
    STRICT_RUN = prev_strict or (persona.get("approval") == "strict")   # stricter only, never looser
    act = _Activity(query, persona, depth)
    try:
        allowed_names, schemas = tools_for(persona)
        model = persona.get("model", DEFAULT_MODEL)
        system = BASE_SYSTEM + "\n\n" + persona.get("system_prompt", "")
        # Prior turns give the agent conversation memory: a follow-up like "and who
        # came second?" resolves against the earlier topic instead of starting cold.
        prior = []
        for h in (history or []):
            role = h.get("role"); content = (h.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                prior.append({"role": role, "content": content[:4000]})
        prior = prior[-20:]                               # cap to keep context bounded
        messages = [{"role":"system","content":system}] + prior + [{"role":"user","content":query}]
        searches = 0
        step = 0
        last_sig = None; repeats = 0
        while True:
            step += 1
            if MAX_STEPS > 0 and step > MAX_STEPS:
                return "[stopped: reached configured step cap MLX_AGENT_MAX_STEPS]"
            act.update("thinking…")
            msg = call_gateway(model, messages, schemas)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                ans = strip_thinking(msg.get("content") or "") or "[no answer]"
                if _looks_degenerate(ans):
                    ans = ("[the model fell into a repetition loop instead of answering — this usually "
                           "means it's too weak at tool-calling for this task. Try /reset and rephrase, "
                           "or switch the persona to the dense qwen3.6-27b coder (MoE models loop here). "
                           "You can also raise MLX_AGENT_FREQ_PENALTY.]")
                # Live-stream the answer to front-ends (e.g. Telegram) ONLY at the top
                # level and ONLY now that generation is safely complete — never during
                # tool turns and never from a subagent (which would interleave garbage).
                if depth == 0 and STREAM_FN is not None and not ans.startswith("[no answer"):
                    try:
                        piece = max(24, len(ans) // 20)
                        for i in range(0, len(ans), piece):
                            STREAM_FN(ans[i:i + piece])
                    except Exception:
                        pass
                return ans
            # Anti-hang guard (NOT a step cap): if the model emits the exact same tool
            # call over and over, it's stuck in a loop — break instead of spinning forever.
            if LOOP_GUARD > 0:
                sig = json.dumps([[tc["function"].get("name"), tc["function"].get("arguments")]
                                  for tc in tool_calls], sort_keys=True)
                repeats = repeats + 1 if sig == last_sig else 0
                last_sig = sig
                if repeats >= LOOP_GUARD:
                    return (f"[stopped: the model repeated the identical tool call {repeats+1}× without "
                            "making progress — it's stuck. Try rephrasing the task, or switch to a dense "
                            "model like qwen3.6-27b (MoE models tend to loop on tool chains). "
                            "Disable this guard with MLX_AGENT_LOOP_GUARD=0.]")
            messages.append({"role":"assistant","content":msg.get("content") or "","tool_calls":tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                act.update(f"tool: {name}")
                if name == "web_search":
                    searches += 1
                    if searches > MAX_SEARCHES:
                        result = ("[search limit reached — STOP searching. Either web_fetch a specific URL you already "
                                  "found to read its real content, or take the direct route (install the package and "
                                  "introspect it, read the source file, or answer with what you have).]")
                        messages.append({"role":"tool","tool_call_id":tc.get("id",""),"name":name,"content":result})
                        if trace_out is not None: trace_out.append(name+"[capped]")
                        continue
                if name == "spawn_subagent":
                    if "spawn_subagent" not in allowed_names:
                        result = "[refused: this persona cannot spawn subagents]"
                    else:
                        try: args = json.loads(tc["function"].get("arguments") or "{}")
                        except json.JSONDecodeError: args = {}
                        result = t_spawn_subagent(depth, spawn_ctx, **args)
                else:
                    result = execute_tool_call(tc, allowed_names)
                if trace_out is not None: trace_out.append(name)
                messages.append({"role":"tool","tool_call_id":tc.get("id",""),
                                 "name":name,"content":result})
    finally:
        STRICT_RUN = prev_strict
        act.done()

def t_spawn_subagent(depth: int, spawn_ctx: dict, persona: str = "", task: str = "", **_) -> str:
    if depth >= MAX_SPAWN_DEPTH:
        return f"[refused: max spawn depth {MAX_SPAWN_DEPTH} reached — do this step yourself]"
    if spawn_ctx.get("count", 0) >= MAX_SPAWNS:
        return f"[refused: spawn budget {MAX_SPAWNS} exhausted — finish with what you have]"
    personas = load_personas()
    sub = personas.get(persona)
    if not sub:
        return f"[no persona '{persona}'. Available: {', '.join(personas)}]"
    spawn_ctx["count"] = spawn_ctx.get("count", 0) + 1
    emit(c(f"\n  ╭─ spawn: {persona}  (depth {depth+1}, subagent #{spawn_ctx['count']})", "magenta"))
    emit(c(f"  │  task: {task[:140]}", "magenta"))
    subtrace = []
    answer = run_agent(task, sub, depth + 1, spawn_ctx, trace_out=subtrace)
    tools_used = ", ".join(subtrace) if subtrace else "none"
    emit(c(f"  ╰─ {persona} done · tools: {tools_used}", "magenta"))
    return f"[subagent '{persona}' finished — tools used: {tools_used}]\n{answer}"

# ─────────────────────────── persona CRUD ─────────────────────────────────
def cmd_list_personas():
    p = load_personas()
    print(c("\nPersonas","bold"), c(f"({PERSONAS_FILE})","dim"))
    for name, cfg in p.items():
        tools = cfg.get("allowed_tools","all")
        tools = "all tools" if tools=="all" else ", ".join(tools)
        strict = "  · STRICT approval" if cfg.get("approval")=="strict" else ""
        print(f"  {c(name,'magenta'):<24} {c(cfg.get('model',''),'cyan')}{c(strict,'yellow')}")
        print(f"    {cfg.get('description','')}  {c('['+tools+']','dim')}")

def _pick_model(current: str = "") -> str:
    models = installed_models()
    if models:
        print("  installed models:")
        for i, m in enumerate(models, 1): print(f"    {i}) {m}")
        raw = input(c(f"  model [number, or type a name{f', blank={current}' if current else ''}]: ","cyan")).strip()
        if not raw and current: return current
        if raw.isdigit() and 1 <= int(raw) <= len(models): return models[int(raw)-1]
        return raw
    return input(c(f"  model name (gateway offline; free-form){f' [{current}]' if current else ''}: ","cyan")).strip() or current

def _multiline(prompt: str, current: str = "") -> str:
    print(c(f"  {prompt} (end with a blank line{'; blank=keep' if current else ''}):","cyan"))
    lines = []
    while True:
        try: line = input("    ")
        except EOFError: break
        if line == "": break
        lines.append(line)
    return "\n".join(lines) if lines else current

def cmd_add_persona():
    p = load_personas()
    name = input(c("persona name (lowercase, e.g. pentester): ","cyan")).strip()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,30}$", name): return print(c("invalid name","red"))
    if name in p and not confirm(f"'{name}' exists — overwrite?"): return
    desc = input(c("  one-line description: ","cyan")).strip()
    model = _pick_model()
    sysp = _multiline("system prompt")
    tools_raw = input(c("  allowed tools [Enter=all, or comma list e.g. web_search,read_file]: ","cyan")).strip()
    allowed = "all" if not tools_raw else [t.strip() for t in tools_raw.split(",") if t.strip()]
    strict = input(c("  approval level [normal / strict] (strict = always ask, no 'allow-all'): ","cyan")).strip().lower()
    entry = {"description":desc,"model":model,"allowed_tools":allowed,"system_prompt":sysp}
    if strict == "strict": entry["approval"] = "strict"
    p[name] = entry
    save_personas(p); print(c(f"✓ saved persona '{name}'","green"))

def cmd_edit_persona(name: str):
    p = load_personas()
    if name not in p: return print(c(f"no persona '{name}' (see --list-personas)","red"))
    cur = p[name]
    print(c(f"editing '{name}' — blank keeps current value","dim"))
    desc = input(c(f"  description [{cur.get('description','')}]: ","cyan")).strip() or cur.get("description","")
    model = _pick_model(cur.get("model",""))
    sysp = _multiline("system prompt", cur.get("system_prompt",""))
    tr = input(c(f"  allowed tools [{cur.get('allowed_tools','all')}] (Enter=keep, 'all', or comma list): ","cyan")).strip()
    allowed = cur.get("allowed_tools","all") if not tr else ("all" if tr=="all" else [t.strip() for t in tr.split(",")])
    cur_appr = cur.get("approval","normal")
    ap = input(c(f"  approval level [{cur_appr}] (normal / strict, Enter=keep): ","cyan")).strip().lower()
    approval = cur_appr if not ap else ap
    entry = {"description":desc,"model":model,"allowed_tools":allowed,"system_prompt":sysp}
    if approval == "strict": entry["approval"] = "strict"
    p[name] = entry
    save_personas(p); print(c(f"✓ updated '{name}'","green"))

def cmd_remove_persona(name: str):
    p = load_personas()
    if name not in p: return print(c(f"no persona '{name}'","red"))
    if not confirm(f"delete persona '{name}'?"): return
    del p[name]; save_personas(p); print(c(f"✓ removed '{name}'","green"))

# ─────────────────────────────── cli ──────────────────────────────────────
def _fmt(s: str) -> str:
    return textwrap.fill(s, width=100) if "\n" not in s and len(s) > 100 else s

def main():
    global AUTO_YES
    ap = argparse.ArgumentParser(description="Persona-aware local MLX tool-executor")
    ap.add_argument("query", nargs="*", help="your task (omit for interactive REPL)")
    ap.add_argument("--persona", default="general", help="persona to run as (see --list-personas)")
    ap.add_argument("--model", default=None, help="override the persona's model for this run")
    ap.add_argument("--yes", action="store_true", help="auto-approve shell/writes (careful)")
    ap.add_argument("--list-personas", action="store_true")
    ap.add_argument("--add-persona", action="store_true")
    ap.add_argument("--edit-persona", metavar="NAME")
    ap.add_argument("--remove-persona", metavar="NAME")
    args = ap.parse_args()
    AUTO_YES = args.yes

    if args.list_personas: return cmd_list_personas()
    if args.add_persona:   return cmd_add_persona()
    if args.edit_persona:  return cmd_edit_persona(args.edit_persona)
    if args.remove_persona:return cmd_remove_persona(args.remove_persona)

    personas = load_personas()
    persona = personas.get(args.persona)
    if persona is None:
        print(c(f"unknown persona '{args.persona}'. Available: {', '.join(personas)}","red"))
        print(c("(use --add-persona to create one, or --persona general)","dim")); return
    if args.model: persona = {**persona, "model": args.model}

    banner = (c("mlx-agent","bold") + c(f"  persona={args.persona}  model={persona.get('model')}  "
              f"gateway={GATEWAY}","dim"))
    if args.query:
        print(banner)
        try: print("\n" + _fmt(run_agent(" ".join(args.query), persona)))
        except requests.HTTPError as e: print(c(f"gateway error: {e} — is :4000 up?","red"))
        return
    print(banner + c("   (type 'exit' to quit)","dim"))
    while True:
        try: q = input(c(f"\n{args.persona} › ","cyan")).strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if q.lower() in ("exit","quit","q"): break
        if not q: continue
        try: print("\n" + _fmt(run_agent(q, persona)))
        except requests.HTTPError as e: print(c(f"gateway error: {e} — is :4000 up? (./mlx-setup.sh --status)","red"))
        except Exception as e: print(c(f"error: {e}","red"))

if __name__ == "__main__":
    main()
