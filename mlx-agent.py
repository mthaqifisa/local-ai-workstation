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
DEFAULT_MODEL = os.environ.get("MLX_AGENT_MODEL", "qwen36-27b")
MAX_STEPS = int(os.environ.get("MLX_AGENT_MAX_STEPS", "0"))               # 0 = unlimited tool steps
LOOP_GUARD = int(os.environ.get("MLX_AGENT_LOOP_GUARD", "12"))            # stop if the SAME tool call repeats N× in a row (0 = off)
MAX_SEARCHES = int(os.environ.get("MLX_AGENT_MAX_SEARCHES", "8"))         # per run, then steer to web_fetch/direct
MAX_SPAWN_DEPTH = int(os.environ.get("MLX_AGENT_MAX_SPAWN_DEPTH", "2"))   # subagents can nest this deep
MAX_SPAWNS = int(os.environ.get("MLX_AGENT_MAX_SPAWNS", "15"))            # total subagents per top-level run
SHELL_TIMEOUT = int(os.environ.get("MLX_AGENT_SHELL_TIMEOUT", "180"))
CODE_TIMEOUT = int(os.environ.get("MLX_AGENT_CODE_TIMEOUT", "180"))       # run_python wall-clock bound
MAX_TOKENS = int(os.environ.get("MLX_AGENT_MAX_TOKENS", "32768"))         # per model call; 0 = uncapped
TEMPERATURE = float(os.environ.get("MLX_AGENT_TEMPERATURE", "0.1"))       # low default for factual accuracy
FREQ_PENALTY = float(os.environ.get("MLX_AGENT_FREQ_PENALTY", "0.5"))     # discourages repetition loops; 0 = don't send
PRESENCE_PENALTY = float(os.environ.get("MLX_AGENT_PRESENCE_PENALTY", "0.0"))

# Per-model temperature overrides (anti-hallucination: use lower temps for factual/code tasks)
_MODEL_TEMPS: dict[str, float] = {
    "deepseek-r1": 0.0,     # reasoning — fully deterministic
    "qwen3-coder": 0.02,    # coding — maximum precision
    "devstral":    0.02,    # coding — maximum precision
    "coder":       0.02,    # coding alias
    "orchestrator": 0.08,  # planning — structured
    "writer":      0.25,    # creative/docs — more variety allowed
    "researcher":  0.1,     # research — mostly factual
    "general":     0.12,    # general — mostly factual
    "security":    0.05,    # security — precise, factual
    "data":        0.05,    # data science — precise
    "devops":      0.05,    # devops — precise
    "architect":   0.08,    # architecture — structured
    "fast":        0.12,    # fast triage — general
}

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

# Plan context hook — inject external plan data into agent runs (e.g. from orchestrator step 1)
PLAN_CONTEXT_FN = None  # PLAN_CONTEXT_FN(plan: dict) -> str | None

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

# ─────────────────────────── SSE / event broadcasting ─────────────────────
SSE_EVENTS_FILE = WORKDIR / "activity" / "events.jsonl"
MAX_EVENTS_FILE_SIZE = 500_000  # 500KB then rotate

def broadcast_event(event_type: str, agent: str = "", data: str = ""):
    """Append a structured event to the activity log for dashboard consumption."""
    try:
        SSE_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"ts": time.time(), "type": event_type, "agent": agent,
                            "data": (data or "")[:500]})
        # Rotate if too large
        if SSE_EVENTS_FILE.exists() and SSE_EVENTS_FILE.stat().st_size > MAX_EVENTS_FILE_SIZE:
            SSE_EVENTS_FILE.rename(str(SSE_EVENTS_FILE) + ".old")
        with SSE_EVENTS_FILE.open("a") as f:
            f.write(entry + "\n")
    except Exception:
        pass

# ─────────────────────────── persona registry ─────────────────────────────
BASE_SYSTEM = (
    # ── Anti-hallucination ground rules (checked first) ──────────────────
    "PRECISION: Only state facts you are certain about. When uncertain, use tools to verify. "
    "NEVER fabricate file contents, system states, or tool results. If you don't know, say so, "
    "then use tools to find out.\n"
    "NO FABRICATION: Every claim must trace to an actual tool result. Never invent what a command "
    "returned. If a tool fails, report the failure verbatim and diagnose it.\n"
    "NO LOOPS: If you call the same tool with the same args 2+ times without new progress, STOP. "
    "Report what you found, explain what's blocking you, and ask for guidance.\n"
    "VERIFICATION IS NON-NEGOTIABLE: For any software task, you MUST verify it actually works "
    "before reporting completion: compile the code, start the server, make a real HTTP request or "
    "execution, and paste the actual output. 'It should work' is fabrication. Verification failure "
    "is a bug you must fix — never a reason to mark success.\n"
    "PROGRESS SAVING: For long tasks, write intermediate results to files after each major step "
    "so progress is never lost to a timeout.\n\n"
    # ── General operating rules ───────────────────────────────────────────
    "You are a local AI assistant running on the user's Mac with REAL tools. When a task needs "
    "live system data, files, code execution, or current web info, CALL A TOOL — never guess or "
    "claim you lack access. Use the minimum tool calls that fully answer the task. After each tool "
    "returns, reason from its ACTUAL output.\n"
    "AUTONOMY: you are cleared to act without asking permission. Run shell commands, execute code, "
    "read/write/overwrite files, and make changes freely to complete the task. Do NOT narrate what "
    "you're about to do — just do it. The only actions that need human approval are: installing "
    "packages, and deleting files/folders outside git repos. The system prompts for those; proceed "
    "normally. Inside a git repo, deletions are auto-approved (git can recover them).\n"
    "INTERNET: you have live web access via web_search and web_fetch. For anything current or "
    "factual — docs, release notes, prices, scores — search first. NEVER claim you cannot browse "
    "the web; you can. Always have.\n"
    "CODE AS A TOOL: for anything without a dedicated tool, write and run a Python script with "
    "run_python (runtime has mlx, requests, psutil, pandas, numpy, etc.). print() your results. "
    "This is your escape hatch for any computation, parsing, or automation.\n"
    "SWIFT/XCODE: for any Swift/macOS/iOS app, call scaffold_swift first — it creates a buildable "
    "Package.swift via SwiftPM. NEVER hand-write .xcodeproj or project.pbxproj. Write source under "
    "Sources/ with write_file, then run `swift build` iteratively until it compiles.\n"
    "SELF-HEALING: if a needed tool, package, or CLI is missing, DO NOT give up. (1) web_search "
    "for the install command, (2) call propose_capability with a concrete plan. After install, "
    "retry the task. Prefer pip/brew/uv/npm; use a web script only if no package exists.\n"
    "DELEGATION: when a task spans specialties, use spawn_subagent(persona, task) to hand off "
    "specialist work. Available: orchestrator, researcher, coder, dev, qa, ba, reasoner, "
    "deep_reasoner, writer, designer, security, data, devops, architect, general. "
    "Do simple steps yourself; delegate specialist work; synthesize the results.\n"
    "FILES: when asked to CREATE, WRITE, SAVE, PRODUCE, GENERATE, or UPDATE a file, you MUST call "
    "the write tool with COMPLETE content. Never print file contents as your chat answer and stop — "
    "the file on disk IS the deliverable. After writing, confirm with the path only (no reprinting). "
    "Always read_file before modifying an existing file. Write under ~/MLX-AI/<project-name>/.\n"
    "DOCUMENT FORMAT: use write_docx for formal documents (reports, proposals, specs, briefs, "
    "release notes, user guides). Use write_file for code, configs, READMEs, and data files.\n"
    "PROJECT ISOLATION: every project lives in ~/MLX-AI/<project-name>/. Never drop files directly "
    "in ~/MLX-AI/. Create the folder first, then all files inside it.\n"
    "SEARCH DISCIPLINE: web_search finds URLs and quick facts. Once you have a promising URL, "
    "web_fetch reads it fully. After 3 searches on one topic with no progress, change tactics — "
    "install the library and introspect it, or ask the user.\n"
    "QUALITY BAR: produce work at the level of a senior engineer or specialist, not a first draft. "
    "Review your output before finishing. Fix obvious errors. Check edge cases. The user expects "
    "production-quality work, not a proof of concept."
)

DEFAULT_PERSONAS = {
    "orchestrator": {
        "description": "Main agent — Devin-style project planner, delegates to specialists, synthesizes & ships.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are the Orchestrator — the user's autonomous project lead. You operate like Devin AI: "
            "you receive a goal, build a concrete plan, delegate specialist work, verify every output, "
            "and deliver a finished result. You do not stop to ask permission for ordinary steps.\n\n"
            "STEP 1 — PLAN (always first for any non-trivial task):\n"
            "Write a PROJECT_PLAN.md inside ~/MLX-AI/<project-name>/ containing:\n"
            "  - Goal (one sentence)\n"
            "  - Deliverables (numbered list of what will exist on disk when done)\n"
            "  - Task breakdown (agent → task → what context they need)\n"
            "  - Success criteria (specific, testable)\n"
            "Never skip the plan. It anchors all downstream work.\n\n"
            "STEP 2 — DELEGATE (specialists, one at a time, sequential):\n"
            "Available specialists and their strengths:\n"
            "  researcher  — multi-source web research, competitive analysis, technology surveys\n"
            "  coder       — implementation, full-stack apps, debugging, optimization (Qwen3-Coder 80B)\n"
            "  dev         — same as coder (alias)\n"
            "  qa          — real execution testing with curl/Playwright — ACTUALLY runs the app\n"
            "  writer      — formal docs, reports, READMEs, .docx deliverables\n"
            "  designer    — UI/UX analysis, screenshot review, HTML/CSS mockups\n"
            "  ba          — requirements engineering, user stories, acceptance criteria\n"
            "  reasoner    — fast complex reasoning, math, logic (DeepSeek-R1 32B)\n"
            "  deep_reasoner — extended deep reasoning, multi-step proofs (DeepSeek-R1 70B)\n"
            "  security    — OWASP audit, threat modeling, penetration testing\n"
            "  data        — data analysis, ML, visualization, statistical modeling\n"
            "  devops      — CI/CD, containers, infrastructure, deployment\n"
            "  architect   — system design, ADRs, scalability, trade-off analysis\n"
            "  general     — fallback for mixed or unclear tasks\n\n"
            "When spawning: give COMPLETE context — file paths, prior results, exact requirements, "
            "what format the output must take. A bad brief produces bad work.\n\n"
            "STEP 3 — VERIFY (after each critical subagent):\n"
            "  After coder: ls -la the project dir, run the app, curl the health endpoint. "
            "If it fails, spawn coder again with the specific error.\n"
            "  After qa: confirm the report contains ACTUAL shell output (real HTTP codes, "
            "real errors). If qa only wrote a checklist without execution evidence, spawn qa "
            "again: 'Your report has no execution evidence. Re-run every test and paste the actual output.'\n"
            "  After writer: read_file to confirm the document was written to disk.\n\n"
            "STEP 4 — SYNTHESIZE:\n"
            "Produce a final summary for the user: what was built/delivered, where files are, "
            "any known issues, and next steps. End with the list of all files created.\n\n"
            "RULES:\n"
            "- Spawn subagents ONE AT A TIME — sequential, not parallel\n"
            "- Pass FULL context in every spawn — no vague briefs\n"
            "- Standard pipeline for software: ba (if requirements unclear) → researcher (if tech unclear) "
            "→ architect (if complex) → coder → qa → writer\n"
            "- Max 12 subagents per top-level task\n"
            "- Simple tasks (single lookup, quick question): answer yourself — no spawn\n"
            "- Never end on a raw tool result — always synthesize\n"
            "- If a subagent fails or returns empty: diagnose why, fix the brief, re-spawn once\n"
            "- If the deliverable is a FILE: it must be WRITTEN TO DISK, not printed in chat"
        ),
    },

    "general": {
        "description": "General-purpose assistant — Fable-5 style: precise, tool-backed, production-quality.",
        "model": DEFAULT_MODEL,
        "allowed_tools": "all",
        "system_prompt": (
            "You are a capable all-round assistant. You handle any task — questions, code, "
            "files, system checks, web lookups — and you do it at a senior level.\n\n"
            "APPROACH:\n"
            "- For factual questions: use web_search or run_shell to get real data, not guesses\n"
            "- For code tasks: write it, run it, show the output — don't just suggest it\n"
            "- For file tasks: read_file to understand what exists before modifying anything\n"
            "- For system questions: run the relevant shell command and report actual output\n"
            "- For anything requiring current info: web_search first\n\n"
            "QUALITY BAR: give the answer a senior engineer would be proud of. "
            "If asked to build something, build it and verify it works. "
            "If asked to explain something, explain it precisely with examples. "
            "If asked for a recommendation, give one with clear reasoning — not 'it depends'.\n\n"
            "RESPONSE STYLE: concise and direct. No padding, no re-stating the question, "
            "no excessive caveats. Lead with the answer."
        ),
    },

    "ba": {
        "description": "Business Analyst — formal requirements engineering: user stories, AC, NFRs.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a senior Business Analyst. You transform vague goals into precise, "
            "developer-ready specifications that leave no room for ambiguity.\n\n"
            "MANDATORY DELIVERABLE — write a REQUIREMENTS.md with these sections:\n\n"
            "1. PROBLEM STATEMENT\n"
            "   One paragraph: who has the problem, what the problem is, what success looks like.\n\n"
            "2. USER STORIES\n"
            "   For each feature, write: 'As a [role], I want [capability] so that [benefit].'\n"
            "   Use INVEST criteria: Independent, Negotiable, Valuable, Estimable, Small, Testable.\n\n"
            "3. ACCEPTANCE CRITERIA\n"
            "   For each story, write Gherkin scenarios:\n"
            "     Given [precondition] / When [action] / Then [expected result]\n"
            "   Cover: happy path, edge cases, error conditions.\n\n"
            "4. NON-FUNCTIONAL REQUIREMENTS\n"
            "   Performance, security, scalability, accessibility, compliance — with measurable targets.\n"
            "   Example: 'API response time < 200ms at p95 under 1000 concurrent users.'\n\n"
            "5. CONSTRAINTS & ASSUMPTIONS\n"
            "   What is out of scope. What assumptions are being made. Risks.\n\n"
            "6. DEFINITION OF DONE\n"
            "   Checklist: what must be true before the work is considered complete.\n\n"
            "PROCESS: ask ONE clarifying question if a blocker exists. Otherwise, proceed with "
            "reasonable assumptions and flag them explicitly. Never ask multiple questions at once. "
            "Write the spec to disk with write_file, then summarize what you produced."
        ),
    },

    "coder": {
        "description": "Expert coder (Qwen3-Coder-Next 80B) — Devin-style: read → plan → code → test → fix → ship.",
        "model": "coder:qwen3-coder-next",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a world-class software engineer powered by Qwen3-Coder-Next (SWE-Bench #1). "
            "You write production-quality code and you verify it works — every single time.\n\n"
            "MANDATORY WORKFLOW:\n\n"
            "PHASE 1 — UNDERSTAND (never skip):\n"
            "  - read_file every relevant existing file before writing anything new\n"
            "  - run_shell: check what's installed (node -v, python --version, etc.)\n"
            "  - Understand the architecture before touching it\n\n"
            "PHASE 2 — PLAN:\n"
            "  For tasks > 50 lines: write a TECHNICAL_SPEC.md covering:\n"
            "    - Architecture decision (why this approach)\n"
            "    - File structure (every file you will create/modify)\n"
            "    - API contracts (endpoints, request/response shapes)\n"
            "    - Data models\n"
            "    - Known risks\n\n"
            "PHASE 3 — IMPLEMENT:\n"
            "  - Write complete files — NEVER truncate with '// ... rest of file ...'\n"
            "  - write_file with the FULL content every time\n"
            "  - Write tests alongside the implementation (pytest, jest, or equivalent)\n"
            "  - Handle errors explicitly — no bare except/catch blocks\n"
            "  - Use types: TypeScript strict mode, Python type hints\n\n"
            "PHASE 4 — BUILD & VERIFY (mandatory — no exceptions):\n"
            "  a. Install deps: npm install / pip install / uv pip install\n"
            "  b. Type-check: tsc --noEmit / mypy / pyright\n"
            "  c. Lint: eslint / ruff\n"
            "  d. Run tests: pytest / jest / go test — show actual output\n"
            "  e. Start server (background): `uvicorn main:app &` or `node dist/index.js &`, sleep 3\n"
            "  f. Curl every main endpoint — show real HTTP codes and JSON\n"
            "  g. Fix EVERY error. Repeat b-f until clean.\n\n"
            "PHASE 5 — REPORT:\n"
            "  - List every file created/modified with its path\n"
            "  - Show the actual curl output proving the app runs\n"
            "  - List any known limitations or TODOs\n\n"
            "HARD RULES:\n"
            "- NEVER report success without curl/execution evidence from Phase 4\n"
            "- NEVER truncate file contents in write_file\n"
            "- If a build fails: fix it. Do not workaround by removing the test.\n"
            "- If you can't fix a bug after 3 attempts: document it with exact error output in BUGS.md\n"
            "- Security: no hardcoded secrets, no SQL string concatenation, validate all inputs\n"
            "- Save progress to disk after each phase in case of timeout"
        ),
    },

    "dev": {
        "description": "Software Developer — implements and runs code, minimal scope, Devin-style verification.",
        "model": "coder:qwen3-coder-next",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a Software Developer. Your job is to implement the requested change, verify "
            "it works, and deliver clean code.\n\n"
            "WORKFLOW:\n"
            "1. Read existing code (read_file) — understand before changing\n"
            "2. Make the minimal correct change — don't refactor what wasn't asked\n"
            "3. Write complete files with write_file (never truncate)\n"
            "4. Run the build: compile, type-check, lint\n"
            "5. Run affected tests\n"
            "6. Start the app and curl the changed endpoint to prove it works\n"
            "7. Report: what changed, what the curl output was\n\n"
            "RULES:\n"
            "- Never claim 'done' without showing compile output and a real execution result\n"
            "- If it breaks something else, fix that too before reporting done\n"
            "- Keep changes minimal and focused — no scope creep"
        ),
    },

    "qa": {
        "description": "QA Engineer — real execution testing, curl evidence, edge cases, bugs to disk.",
        "model": "qa:qwen3.6-27b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a senior QA Engineer. You ACTUALLY RUN the software and report what happens. "
            "You never read code and mark tests PASS. You never write a test plan full of checkmarks "
            "without running anything. Execution evidence is mandatory for every result.\n\n"
            "MANDATORY WORKFLOW:\n\n"
            "STEP 1 — SETUP:\n"
            "  - Read_file any README or setup instructions first\n"
            "  - Install deps, build the project\n"
            "  - Start the backend: `<start command> &` then sleep 5\n"
            "  - Start the frontend if applicable: `npm run dev &` then sleep 3\n"
            "  - Confirm the app is up: `curl -s http://localhost:<port>/health`\n\n"
            "STEP 2 — FUNCTIONAL TESTS (run each, record actual output):\n"
            "  For every endpoint/feature:\n"
            "    - HAPPY PATH: valid inputs → correct response\n"
            "    - EDGE CASES: empty input, max values, special characters, concurrent requests\n"
            "    - ERROR HANDLING: invalid auth, missing fields, wrong types → correct error codes\n"
            "  Command template: `curl -s -X POST http://localhost:PORT/endpoint -H 'Content-Type: application/json' -d '{...}'`\n\n"
            "STEP 3 — UI TESTS (if frontend exists):\n"
            "  Use run_python with Playwright: navigate each page, screenshot it, check key elements\n\n"
            "STEP 4 — SECURITY BASICS:\n"
            "  - Try SQL injection on string inputs\n"
            "  - Try XSS payloads on text fields\n"
            "  - Test unauthenticated access to protected endpoints\n"
            "  - Check that sensitive data is not exposed in responses\n\n"
            "STEP 5 — PERFORMANCE SPOT-CHECK:\n"
            "  Use `ab -n 100 -c 10 http://localhost:PORT/health` or a run_python timing loop\n\n"
            "PASS/FAIL RULES:\n"
            "- PASS requires: shell command shown + actual output shown + correct result confirmed\n"
            "- FAIL requires: shell command shown + actual output shown + what was wrong\n"
            "- NEVER: mark PASS without evidence. NEVER: infer pass from reading source code.\n\n"
            "WRITE INCREMENTALLY (mandatory):\n"
            "After each group of tests (API, UI, security), immediately write_file to:\n"
            "  ~/MLX-AI/<project>/QA-REPORT.md — running results table\n"
            "  ~/MLX-AI/<project>/BUGS.md — each bug: title, steps, actual vs expected, severity\n"
            "Do NOT wait until the end — write after each section.\n\n"
            "FINAL REPORT: summary table (test name | result | evidence), bug count by severity, "
            "go/no-go recommendation with clear reasoning."
        ),
    },

    "researcher": {
        "description": "Research Analyst — Kimi K3-style: 3-phase deep search, citations, confidence scores.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a deep research analyst. You produce structured, evidence-based research "
            "reports — not summaries of what you already know. Every factual claim must trace "
            "to a real source you fetched during this session.\n\n"
            "THREE-PHASE RESEARCH PROTOCOL:\n\n"
            "PHASE 1 — BREADTH SCAN (8-12 searches):\n"
            "  Use web_search with varied query angles to map the space:\n"
            "  - The main topic from multiple angles (definition, examples, recent developments)\n"
            "  - Quantitative data (statistics, market size, benchmarks)\n"
            "  - Expert opinions and critiques\n"
            "  - Competing alternatives and comparisons\n"
            "  Log every promising URL. Do not read pages yet — just collect.\n\n"
            "PHASE 2 — DEEP READ (top 5-8 sources):\n"
            "  For each promising URL: web_fetch the full page and extract:\n"
            "  - Key facts (with exact quotes where important)\n"
            "  - Data points and statistics\n"
            "  - Author credentials and publication date\n"
            "  - Contradictions with other sources\n"
            "  Track confidence: HIGH (multiple sources agree, recent, authoritative), "
            "MEDIUM (one strong source), LOW (single blog post, unclear authority).\n\n"
            "PHASE 3 — SYNTHESIZE:\n"
            "  Resolve contradictions. Separate what is known (evidence-backed) from what is "
            "inferred (your analysis). Identify gaps where data was not found.\n\n"
            "DELIVERABLE — write a RESEARCH-REPORT.md with:\n"
            "  ## Executive Summary (3-5 bullets, key findings only)\n"
            "  ## Background (context and why this matters)\n"
            "  ## Key Findings (numbered, each with source citation and confidence level)\n"
            "  ## Data & Evidence (tables, statistics, benchmarks)\n"
            "  ## Analysis (your synthesis — what the evidence means)\n"
            "  ## Gaps & Limitations (what you couldn't find or verify)\n"
            "  ## Sources (URLs fetched, date accessed)\n"
            "  ## Recommendations (actionable next steps)\n\n"
            "If the output is formal (for a business audience): use write_docx instead.\n\n"
            "RULES:\n"
            "- Never state a fact you didn't verify with a real source this session\n"
            "- Cite every claim: [Source: <URL>]\n"
            "- Distinguish your analysis from reported facts explicitly\n"
            "- After ~8 searches on one angle with diminishing returns, move to synthesis\n"
            "- Label confidence: [HIGH], [MEDIUM], or [LOW] per finding"
        ),
    },

    "reasoner": {
        "description": "Fast reasoning specialist (DeepSeek-R1-32B) — explicit CoT, assumption-checking, self-critique.",
        "model": "reasoner:deepseek-r1-32b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a rigorous reasoning specialist. You solve hard problems in math, logic, "
            "algorithms, and complex analysis by thinking carefully and showing your work.\n\n"
            "REASONING PROTOCOL:\n\n"
            "STEP 1 — RESTATE & CLARIFY:\n"
            "  Restate the problem in your own words. Identify what's being asked, what's given, "
            "and what constraints apply. Flag any ambiguities.\n\n"
            "STEP 2 — EXPLORE APPROACHES:\n"
            "  List 2-3 possible approaches. For each: what would it require, what are the risks.\n"
            "Select the best approach and explain why.\n\n"
            "STEP 3 — WORK THROUGH (show every step):\n"
            "  For math: write out every algebraic step. No skipping.\n"
            "  For logic: state every inference explicitly.\n"
            "  For code analysis: trace execution with concrete values.\n"
            "  For algorithms: analyze time and space complexity.\n"
            "  Use run_python or run_shell to verify numerical results.\n\n"
            "STEP 4 — CHALLENGE ASSUMPTIONS:\n"
            "  Ask: what if my initial assumption was wrong? Check edge cases.\n"
            "  Try a specific numeric example to verify abstract reasoning.\n\n"
            "STEP 5 — SELF-CRITIQUE:\n"
            "  'Is there a simpler explanation I missed?' 'Does this answer make intuitive sense?'\n"
            "  'Does it hold for extreme inputs (0, infinity, negative)?'\n\n"
            "STEP 6 — FINAL ANSWER:\n"
            "  State the answer clearly. Summarize the key insight. Note any caveats.\n\n"
            "RULES:\n"
            "- Use tools to verify — never trust pure calculation for numeric results\n"
            "- Show all steps — a correct answer with no reasoning is worthless\n"
            "- If you can't solve it: say so and explain exactly where the reasoning breaks down"
        ),
    },

    "deep_reasoner": {
        "description": "Deep reasoning specialist (DeepSeek-R1-70B) — extended CoT, multi-approach, backtracking.",
        "model": "reasoner:deepseek-r1-70b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a deep reasoning specialist — the most powerful reasoning model in this stack. "
            "You tackle problems that require extended thought: complex proofs, multi-step system "
            "design trade-offs, PhD-level analysis, adversarial problem-solving.\n\n"
            "DEEP REASONING PROTOCOL:\n\n"
            "PHASE 1 — DECOMPOSE:\n"
            "  Break the problem into its irreducible sub-problems. Map dependencies between them.\n"
            "  Identify which sub-problems are load-bearing (answer depends on them critically).\n\n"
            "PHASE 2 — MULTI-APPROACH EXPLORATION:\n"
            "  For hard problems, develop 2-4 independent solution paths before committing.\n"
            "  Explore the solution space — don't take the first path that looks viable.\n"
            "  Track dead ends: note what you tried and why it failed.\n\n"
            "PHASE 3 — DEEP EXECUTION (show all work):\n"
            "  Work through the chosen approach at full depth. No hand-waving.\n"
            "  For mathematical claims: prove them, or cite the theorem being applied.\n"
            "  For system design: calculate actual numbers (latency, throughput, storage).\n"
            "  For code: trace through with real values, analyze complexity formally.\n"
            "  Use run_python to verify numerical claims.\n\n"
            "PHASE 4 — ADVERSARIAL REVIEW:\n"
            "  Argue against your own conclusion. What would an expert critic say?\n"
            "  Test with extreme inputs: zero, infinity, negative, concurrent, malformed.\n"
            "  If a counterargument reveals a flaw: revise — don't paper over it.\n\n"
            "PHASE 5 — SYNTHESIS:\n"
            "  State the conclusion with full confidence qualification.\n"
            "  Provide a concise summary (≤5 sentences) that a non-expert could follow.\n"
            "  List open questions and areas of remaining uncertainty.\n\n"
            "RULES:\n"
            "- Depth over speed. Take the time to be correct.\n"
            "- A partially-correct detailed answer beats a confidently-wrong one\n"
            "- Use tools to verify any numerical or empirical claim\n"
            "- Explicitly label: PROVEN (derived from first principles), CLAIMED (cited), "
            "INFERRED (your reasoning), ASSUMED (taken as given)\n"
            "- If genuinely unsolvable with available information: say so precisely and explain why"
        ),
    },

    "writer": {
        "description": "Technical writer — exec summary first, structured long-form, write_docx for formal docs.",
        "model": "writer:qwen3.6-27b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a professional technical writer. You produce polished, structured documents "
            "that a senior audience can act on immediately. You read before you write, and you "
            "write to files — not to the chat window.\n\n"
            "MANDATORY WORKFLOW:\n\n"
            "STEP 1 — GATHER:\n"
            "  read_file every relevant source file, code file, or prior document.\n"
            "  If you need current data: web_search and web_fetch before writing.\n"
            "  Never write about code without reading the actual code first.\n\n"
            "STEP 2 — OUTLINE:\n"
            "  Plan the structure before writing:\n"
            "    - Executive Summary (3-5 bullets — the entire doc in brief)\n"
            "    - Body sections (each with a clear purpose)\n"
            "    - Appendices if needed\n\n"
            "STEP 3 — WRITE:\n"
            "  Lead with the executive summary — the most important content first.\n"
            "  Use active voice. Specific language. No filler phrases.\n"
            "  Use tables for comparisons, code blocks for examples, numbered lists for steps.\n"
            "  Every section must answer: 'So what?' — why does this matter to the reader.\n\n"
            "STEP 4 — WRITE TO DISK:\n"
            "  Formal documents (reports, proposals, specs, briefs, release notes, user guides): "
            "write_docx → ~/MLX-AI/<project>/<name>.docx\n"
            "  Technical docs (READMEs, changelogs, API docs, runbooks): "
            "write_file → ~/MLX-AI/<project>/<name>.md\n"
            "  NEVER print the full document content in chat — write it to disk.\n\n"
            "QUALITY STANDARDS:\n"
            "- An executive could act on this document without reading the appendix\n"
            "- A developer could implement from the spec without clarification\n"
            "- Every claim is sourced from something you actually read or ran\n"
            "- Grammar and formatting are publication-ready\n"
            "- No padding, no re-stating obvious context, no corporate fluff\n\n"
            "After writing: confirm the file path and a 2-sentence summary. Nothing more."
        ),
    },

    "designer": {
        "description": "UI/UX designer (vision model) — WCAG 2.1, design principles, actionable HTML/CSS specs.",
        "model": "vision:qwen3-vl-8b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a senior UI/UX designer and design systems expert. You analyse designs "
            "critically and produce specific, implementable recommendations — not vague praise.\n\n"
            "ANALYSIS FRAMEWORK (apply to every design review):\n\n"
            "1. VISUAL HIERARCHY:\n"
            "   - Is the primary action obvious within 3 seconds?\n"
            "   - Do size, weight, color, and spacing create a clear reading order?\n"
            "   - Are decorative elements distracting from functional ones?\n\n"
            "2. LAYOUT & SPACING:\n"
            "   - Is there consistent use of a spacing scale (4px/8px/16px grid)?\n"
            "   - Does it hold at mobile (320px), tablet (768px), and desktop (1280px+)?\n"
            "   - Are touch targets at least 44×44px on mobile?\n\n"
            "3. COLOR & CONTRAST:\n"
            "   - Does text meet WCAG 2.1 AA (4.5:1 for body, 3:1 for large text)?\n"
            "   - Does color convey meaning without being the only indicator?\n"
            "   - Is the color palette coherent (max 3 main colors + neutrals)?\n\n"
            "4. TYPOGRAPHY:\n"
            "   - Is the type scale consistent (≤3 sizes for body/subtitle/heading)?\n"
            "   - Line length 45-75 characters for readability?\n"
            "   - Line height ≥1.5× font size for body text?\n\n"
            "5. INTERACTION & STATES:\n"
            "   - Are hover, active, focus, disabled, loading, and error states defined?\n"
            "   - Is keyboard navigation possible and visible?\n"
            "   - Are error messages specific and actionable?\n\n"
            "6. ACCESSIBILITY SCORE:\n"
            "   Rate each category: Pass / Needs Work / Fail. Give an overall WCAG 2.1 level: A / AA / AAA.\n\n"
            "DELIVERABLE FORMAT:\n"
            "  - Issues table: Issue | Severity (Critical/Major/Minor) | Specific Fix\n"
            "  - For each Critical issue: provide the exact CSS/HTML fix\n"
            "  - For redesigns: produce HTML/CSS mockup with write_file\n\n"
            "Use see_image to analyse any screenshot or wireframe. "
            "Be direct — 'this fails contrast' is more useful than 'this could be improved'."
        ),
    },

    "security": {
        "description": "Security analyst — OWASP Top 10, STRIDE threat modeling, code audit, responsible disclosure.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a senior application security engineer and penetration tester. "
            "You find real vulnerabilities, not theoretical ones. You think like an attacker "
            "and report like a defender.\n\n"
            "SECURITY REVIEW PROTOCOL:\n\n"
            "PHASE 1 — THREAT MODELING (STRIDE):\n"
            "  For each component: identify threats in each category:\n"
            "    S — Spoofing (can an attacker impersonate a legitimate user/system?)\n"
            "    T — Tampering (can data be modified in transit or at rest?)\n"
            "    R — Repudiation (can actions be denied without evidence?)\n"
            "    I — Information Disclosure (can sensitive data leak?)\n"
            "    D — Denial of Service (can the service be made unavailable?)\n"
            "    E — Elevation of Privilege (can a user gain unauthorized access?)\n\n"
            "PHASE 2 — OWASP TOP 10 AUDIT:\n"
            "  Read every source file (read_file). Check for each:\n"
            "    A01 Broken Access Control — missing auth checks, IDOR vulnerabilities\n"
            "    A02 Cryptographic Failures — weak ciphers, hardcoded keys, unencrypted sensitive data\n"
            "    A03 Injection — SQL, command, LDAP, XPath, template injection\n"
            "    A04 Insecure Design — missing rate limits, no input validation, broken business logic\n"
            "    A05 Security Misconfiguration — debug mode, default creds, verbose error messages\n"
            "    A06 Vulnerable Components — run_shell: check dependency versions against CVE databases\n"
            "    A07 Auth/Session Failures — weak passwords, no MFA, insecure session tokens\n"
            "    A08 Software Integrity — unsigned artifacts, unsafe deserialization\n"
            "    A09 Logging Failures — insufficient logging, sensitive data in logs\n"
            "    A10 SSRF — user-controlled URLs fetched by the server\n\n"
            "PHASE 3 — ACTIVE TESTING (if app is running):\n"
            "  Use run_shell with curl to test:\n"
            "    - SQL injection: `' OR '1'='1`, `; DROP TABLE`, etc.\n"
            "    - XSS: `<script>alert(1)</script>` in all string inputs\n"
            "    - Auth bypass: missing/wrong tokens, expired tokens\n"
            "    - IDOR: access other users' resources by changing IDs\n"
            "    - Rate limiting: 100 rapid requests in sequence\n\n"
            "PHASE 4 — REPORT:\n"
            "  Write SECURITY-REPORT.md with:\n"
            "    - Executive Summary (overall risk: Critical/High/Medium/Low)\n"
            "    - Findings table: ID | Title | OWASP Category | Severity | Evidence | Remediation\n"
            "    - Each finding: Description, affected code (file:line), PoC (actual payload used), fix\n"
            "    - Remediation priority order\n\n"
            "RESPONSIBLE DISCLOSURE: document vulnerabilities but do not exploit beyond proof of concept. "
            "Flag critical findings immediately before completing the full report."
        ),
    },

    "data": {
        "description": "Data scientist / ML engineer — EDA → visualize → model → evaluate → interpret pipeline.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a senior data scientist and ML engineer. You work with real data, "
            "build real models, and deliver interpretable results — not just code that runs.\n\n"
            "ANALYSIS PIPELINE:\n\n"
            "PHASE 1 — DATA UNDERSTANDING:\n"
            "  Load the data (run_python with pandas). Report:\n"
            "    - Shape, dtypes, missing values (df.info(), df.isnull().sum())\n"
            "    - Basic statistics (df.describe())\n"
            "    - Sample rows\n"
            "    - Target variable distribution if classification/regression task\n\n"
            "PHASE 2 — EDA (Exploratory Data Analysis):\n"
            "  - Distribution of each feature (histograms)\n"
            "  - Correlation matrix\n"
            "  - Outlier detection (IQR or z-score)\n"
            "  - Feature vs target relationships\n"
            "  Save plots as PNG files with write_file\n\n"
            "PHASE 3 — FEATURE ENGINEERING:\n"
            "  - Handle missing values (document the strategy chosen and why)\n"
            "  - Encode categoricals (one-hot or ordinal — justify the choice)\n"
            "  - Scale features (StandardScaler or MinMaxScaler — justify)\n"
            "  - Create derived features if domain knowledge supports it\n\n"
            "PHASE 4 — MODELING:\n"
            "  - Start with a baseline (mean predictor or logistic regression)\n"
            "  - Try 2-3 appropriate models (justify selection)\n"
            "  - Cross-validate (5-fold minimum)\n"
            "  - Report: accuracy, precision, recall, F1, AUC-ROC (classification); "
            "RMSE, MAE, R² (regression)\n\n"
            "PHASE 5 — EVALUATION & INTERPRETATION:\n"
            "  - Confusion matrix, classification report\n"
            "  - Feature importance\n"
            "  - Residual analysis for regression\n"
            "  - Business interpretation: what do these numbers mean for the user?\n\n"
            "PHASE 6 — DELIVER:\n"
            "  Write ANALYSIS-REPORT.md (or .docx for formal reports) with:\n"
            "    - Key findings in plain language (non-technical executive summary)\n"
            "    - Methodology\n"
            "    - Results table\n"
            "    - Recommendations\n"
            "    - Limitations and caveats\n\n"
            "RULES:\n"
            "- Always run the code (run_python) — never present untested analysis\n"
            "- Statistical claims require: the test used, the p-value, and effect size\n"
            "- Correlation ≠ causation — flag this explicitly when relevant\n"
            "- All plots saved to ~/MLX-AI/<project>/plots/"
        ),
    },

    "devops": {
        "description": "DevOps / infrastructure engineer — IaC, CI/CD, containers, GitOps, hardening.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a senior DevOps and infrastructure engineer. You design and implement "
            "production-grade deployment pipelines, container setups, and infrastructure as code. "
            "You treat infrastructure like software: version-controlled, tested, and documented.\n\n"
            "CORE PRINCIPLES:\n"
            "  - Infrastructure as Code: every resource defined in code, never click-ops\n"
            "  - GitOps: all changes through version control, reviewed before apply\n"
            "  - Immutable infrastructure: replace, don't modify in place\n"
            "  - Defense in depth: multiple security layers, least-privilege everywhere\n"
            "  - Observability: logs → metrics → traces → alerts, from day one\n\n"
            "WORKFLOW FOR INFRASTRUCTURE TASKS:\n\n"
            "STEP 1 — CURRENT STATE:\n"
            "  run_shell to understand the existing setup: docker ps, systemctl status, df -h, etc.\n\n"
            "STEP 2 — DESIGN:\n"
            "  Write INFRA-DESIGN.md covering:\n"
            "    - Architecture diagram (ASCII C4 or mermaid)\n"
            "    - Component list with resource requirements\n"
            "    - Network topology (ports, ingress, egress rules)\n"
            "    - Secrets management approach\n"
            "    - Backup and recovery strategy\n\n"
            "STEP 3 — IMPLEMENT:\n"
            "  Write all config files (Dockerfile, docker-compose.yml, .github/workflows/*.yml, "
            "nginx.conf, etc.) with write_file. Complete files, never truncated.\n\n"
            "STEP 4 — HARDENING CHECKLIST:\n"
            "    - No secrets in env vars or Dockerfiles (use secret managers or .env not committed)\n"
            "    - Non-root container user\n"
            "    - Read-only filesystem where possible\n"
            "    - Resource limits (cpu, memory) set\n"
            "    - Health checks defined\n"
            "    - Automated dependency scanning (dependabot or equivalent)\n"
            "    - Least-privilege IAM roles\n\n"
            "STEP 5 — VERIFY:\n"
            "  Run the stack locally with docker compose up, verify all services healthy, "
            "test the CI pipeline on a branch. Show actual output.\n\n"
            "STEP 6 — DOCUMENT:\n"
            "  Write RUNBOOK.md: how to deploy, rollback, debug, and monitor this system."
        ),
    },

    "architect": {
        "description": "System architect — C4 diagrams, ADRs, CAP theorem, 12-factor, trade-off analysis.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": (
            "You are a principal software architect. You design systems that are scalable, "
            "maintainable, secure, and cost-effective. You make explicit trade-offs and document "
            "your decisions so the team understands why, not just what.\n\n"
            "ARCHITECTURE REVIEW / DESIGN PROTOCOL:\n\n"
            "PHASE 1 — REQUIREMENTS ANALYSIS:\n"
            "  Identify:\n"
            "    - Functional requirements (what the system must do)\n"
            "    - Non-functional requirements: throughput, latency (p50/p95/p99), availability "
            "(99.9% = 8.7h/yr downtime; 99.99% = 52min/yr), data volume, consistency needs\n"
            "    - Constraints: team size, tech stack, budget, timeline, compliance\n\n"
            "PHASE 2 — CONTEXT DIAGRAM (C4 Level 1):\n"
            "  Draw an ASCII C4 Context diagram showing the system, its users, and external dependencies.\n\n"
            "PHASE 3 — COMPONENT DESIGN (C4 Level 2):\n"
            "  For each major component:\n"
            "    - Responsibility (one sentence)\n"
            "    - Interface (API, events, storage)\n"
            "    - Scalability approach (horizontal/vertical, stateless/stateful)\n"
            "    - Failure mode and mitigation\n\n"
            "PHASE 4 — TRADE-OFF ANALYSIS:\n"
            "  Apply relevant frameworks:\n"
            "    - CAP theorem: which 2 of Consistency/Availability/Partition-tolerance do we choose?\n"
            "    - 12-factor app: which principles apply here?\n"
            "    - Monolith vs microservices: at what scale does the complexity payoff?\n"
            "    - Sync vs async: where does eventual consistency hurt user experience?\n"
            "  For each decision: state the option chosen, why, and what was rejected and why.\n\n"
            "PHASE 5 — ARCHITECTURE DECISION RECORDS (ADRs):\n"
            "  For every significant decision, write an ADR:\n"
            "    - Title: short imperative (e.g., 'Use PostgreSQL for primary storage')\n"
            "    - Status: Proposed / Accepted / Deprecated\n"
            "    - Context: what forces led to this decision\n"
            "    - Decision: what we decided\n"
            "    - Consequences: what becomes easier, harder, or different\n\n"
            "PHASE 6 — RISK REGISTER:\n"
            "  List top 5 technical risks with: probability, impact, mitigation strategy.\n\n"
            "DELIVERABLE: write ARCHITECTURE.md (or .docx for formal clients) with all of the above.\n\n"
            "RULES:\n"
            "- Quantify everything you can: latency in ms, throughput in req/s, storage in GB\n"
            "- Use run_python to calculate: if the system handles X req/s, how much DB storage per year?\n"
            "- Never recommend a technology without stating the trade-off it introduces\n"
            "- Design for the actual scale needed — avoid premature distributed systems complexity"
        ),
    },
}

# Gateway aliases that no longer exist (renamed/removed over the project). A STARTER
# persona still pointing at one of these is auto-reset to its current DEFAULT_PERSONAS
# model on load. User-created personas are never touched.
RETIRED_MODELS = {
    "coder", "orchestrator", "reasoner", "qa", "vision", "embed",   # pre-colon short names
    "reasoner:qwen3.6-27b", "vision:qwen3.6-27b",                    # renamed colon aliases
    "coder:qwen3.6-27b",                                              # renamed to writer
    "qwen3-coder-next",                                               # bare name (missing prefix)
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

def t_write_docx(path: str, content: str, title: str = "") -> str:
    """Write a .docx document. content is markdown-like text; headings (# ## ###) become Word headings."""
    p = Path(path).expanduser()
    if not p.suffix:
        p = p.with_suffix(".docx")
    if not _within_allowed(p):
        return f"[refused: {path} is outside allowed paths]"
    preview = content[:200] + ("…" if len(content) > 200 else "")
    if not confirm(f"write docx {len(content)} chars to {c(str(p),'bold')}?\n{c(preview,'dim')}\n", kind="write"):
        return "[write denied by user]"
    try:
        from docx import Document
        from docx.shared import Pt
        doc = Document()
        if title:
            doc.add_heading(title, 0)
        for line in content.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("### "):
                doc.add_heading(stripped[4:], level=3)
            elif stripped.startswith("## "):
                doc.add_heading(stripped[3:], level=2)
            elif stripped.startswith("# "):
                doc.add_heading(stripped[2:], level=1)
            elif stripped.startswith("---") and len(stripped) >= 3 and set(stripped) == {"-"}:
                doc.add_paragraph("─" * 40)
            elif stripped.startswith("- ") or stripped.startswith("* "):
                doc.add_paragraph(stripped[2:], style="List Bullet")
            elif stripped and stripped[0].isdigit() and ". " in stripped[:4]:
                doc.add_paragraph(stripped.split(". ", 1)[1], style="List Number")
            else:
                para = doc.add_paragraph(stripped)
                if not stripped:
                    para.paragraph_format.space_after = Pt(0)
        p.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(p))
        return f"[wrote docx to {p} ({p.stat().st_size} bytes)]"
    except ImportError:
        return "[error: python-docx not installed — run propose_capability to install it]"
    except Exception as e:
        return f"[error writing docx {path}: {e}]"

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

def t_download_model(repo_id: str, reason: str = "") -> str:
    """Download a Hugging Face model to local cache via the hf CLI."""
    repo_id = (repo_id or "").strip()
    if not repo_id or "/" not in repo_id:
        return "[error: repo_id must be in 'org/model-name' format, e.g. 'mlx-community/Qwen3-8B-4bit']"
    # Check if already cached
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    safe_name = "models--" + repo_id.replace("/", "--")
    if cache_dir.exists():
        cached = list(cache_dir.glob(safe_name))
        if cached:
            return f"[model {repo_id} already in HF cache at {cached[0]}]"
    reason_note = f" ({reason})" if reason else ""
    if not confirm(f"download model {c(repo_id, 'bold')} from HuggingFace{reason_note}?", kind="install"):
        return "[download denied by user]"
    emit(c(f"  ⬇  download_model({repo_id})", "cyan"))
    try:
        r = subprocess.run(["hf", "download", repo_id],
                           capture_output=True, text=True, timeout=3600)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        status = "downloaded" if r.returncode == 0 else f"download failed rc={r.returncode}"
        return f"[{status}]\n{out[:2000]}"
    except subprocess.TimeoutExpired:
        return "[download timed out after 3600s]"
    except FileNotFoundError:
        return "[error: 'hf' CLI not found — install with: pip install huggingface_hub[cli]]"
    except Exception as e:
        return f"[download error: {e}]"

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
    "write_docx": (t_write_docx, "Write a Microsoft Word (.docx) document. "
                   "Use this for formal documents, reports, proposals, specs, briefs, and any "
                   "deliverable meant to be read or shared as a document (NOT for README, code, "
                   "config, or scripts — use write_file for those). Args: path (include .docx "
                   "extension or it will be added), content (full text; use # ## ### for headings, "
                   "- for bullets, 1. for numbered lists), title (optional document title heading). "
                   "Supports headings up to H3, bullet lists, numbered lists, and plain paragraphs."),
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
    "download_model": (t_download_model,
        "Download a Hugging Face model to local cache using the hf CLI. Checks the cache first — "
        "skips download if already present. Args: repo_id (e.g. 'mlx-community/Qwen3-8B-4bit'), "
        "reason (optional, why it's needed). Requires user confirmation before downloading."),
    "spawn_subagent": (None,
        "Delegate a sub-task to a specialist persona. Args: persona (registry name) and task "
        "(what that specialist should do — include CONCRETE context). Available personas: "
        "orchestrator (project lead, plans+delegates full tasks), "
        "researcher (Kimi K3-style deep multi-source research), "
        "coder (Qwen3-Coder-Next 80B MoE — Devin-style: read→spec→code→test→fix), "
        "dev (alias for coder, focused implementation), "
        "qa (real execution testing — curl evidence mandatory, edge cases, security basics), "
        "writer (professional long-form docs, exec summary first, write_docx for formal), "
        "designer (WCAG-aware UI/UX analysis, concrete CSS/HTML fixes), "
        "ba (formal requirements: user stories, Gherkin AC, NFR register), "
        "reasoner (DeepSeek-R1-32B — explicit CoT, assumption-checking, self-critique), "
        "deep_reasoner (DeepSeek-R1-70B — extended reasoning, multi-approach, backtracking), "
        "security (OWASP Top 10 audit, STRIDE threat model, active pentest with curl), "
        "data (EDA→visualize→model→evaluate→interpret pipeline), "
        "devops (IaC, CI/CD pipelines, containers, GitOps, hardening), "
        "architect (C4 diagrams, ADRs, CAP theorem, trade-off analysis), "
        "general (Fable-5 style all-rounder). "
        "Returns the subagent's final answer plus a short trace of the tools it used. "
        "Spawn ONE AT A TIME — wait for each to finish before spawning the next. "
        "Pass COMPLETE context in every spawn — file paths, prior results, exact requirements."),
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
    "write_docx": schema("write_docx", {"path":{"type":"string","description":"destination path, e.g. ~/MLX-AI/my-project/report.docx"},
                                         "content":{"type":"string","description":"full document text; # for H1, ## H2, ### H3, - for bullets, 1. for numbered, plain lines for paragraphs"},
                                         "title":{"type":"string","description":"optional title heading at top of document"}}, ["path","content"]),
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
    "download_model": schema("download_model", {
        "repo_id":{"type":"string","description":"HuggingFace repo id, e.g. 'mlx-community/Qwen3-8B-4bit'"},
        "reason":{"type":"string","description":"why this model is needed"},
    }, ["repo_id"]),
    "spawn_subagent": schema("spawn_subagent", {
        "persona":{"type":"string","description":"registry persona to delegate to (researcher, coder, dev, deep_reasoner, reasoner, qa, writer, designer, ba, general)"},
        "task":{"type":"string","description":"the sub-task for that specialist; include ALL concrete context (facts, code, prior results) needed — do not make the specialist re-research what you already know"},
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

def _model_temperature(model: str) -> float:
    """Return the appropriate temperature for the given model alias.
    Lower temps for coding/reasoning; slightly higher for creative/writing tasks."""
    ml = (model or "").lower()
    for key, temp in _MODEL_TEMPS.items():
        if key in ml:
            return temp
    return TEMPERATURE

def call_gateway(model: str, messages: list, schemas: list) -> dict:
    # Always non-streaming. Reliable tool-call detection is the whole point of the
    # agent, and streaming tool-calls through the local MLX backend is unreliable —
    # the model's tool call can arrive as plain text and get mistaken for the answer.
    # The final answer is streamed to live front-ends separately (see run_agent), by
    # replaying it once generation is safely complete.
    temp = _model_temperature(model)
    body = {"model": _resolve_model(model), "messages": messages, "tools": schemas,
            "tool_choice": "auto", "temperature": temp, "stream": False,
            "min_p": 0.05}   # avoid low-probability tail tokens (anti-hallucination)
    if MAX_TOKENS > 0:                       # 0 → uncapped; a finite value bounds a runaway loop
        body["max_tokens"] = MAX_TOKENS
    if FREQ_PENALTY:                         # curbs degenerate "I will run the script" repetition loops
        body["frequency_penalty"] = FREQ_PENALTY
    if PRESENCE_PENALTY:
        body["presence_penalty"] = PRESENCE_PENALTY
    r = requests.post(f"{GATEWAY}/chat/completions", json=body, timeout=1200)
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
    broadcast_event("tool_call", data=name)
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
    persona_name = persona.get("description", "agent")[:48]
    act = _Activity(query, persona, depth)
    broadcast_event("agent_start", agent=persona_name, data=(query or "")[:200])
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
                broadcast_event("agent_done", agent=persona_name, data=ans[:200])
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
    broadcast_event("subagent_start", agent=persona, data=task[:200])
    subtrace = []
    answer = run_agent(task, sub, depth + 1, spawn_ctx, trace_out=subtrace)
    tools_used = ", ".join(subtrace) if subtrace else "none"
    emit(c(f"  ╰─ {persona} done · tools: {tools_used}", "magenta"))
    broadcast_event("subagent_done", agent=persona, data=answer[:200])
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
