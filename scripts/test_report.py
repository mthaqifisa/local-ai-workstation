#!/usr/bin/env python3
"""Run every test layer and write docs/TEST-REPORT.md from the raw results (never hand-edited).

Layers: lint (ruff), types (mypy --strict), unit + wipe tests, live integration tests, installer
idempotence, and the latest `law eval` run. Raw JSON/XML lands in ~/LAW/outputs/test-artifacts/.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAW_HOME = Path(os.environ.get("LAW_HOME", Path.home() / "LAW"))
ART = LAW_HOME / "outputs" / "test-artifacts"
VENV_BIN = LAW_HOME / "runtime" / "law-venv" / "bin"


def run(cmd: list[str], timeout: float = 3600) -> tuple[int, str, float]:
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip(), round(time.monotonic() - t0, 1)


def junit(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()  # noqa: S314 - our own pytest output, not untrusted input
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None
    return {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}


def pytest_layer(name: str, args: list[str]) -> dict[str, object]:
    xml = ART / f"junit-{name}.xml"
    code, out, secs = run([str(VENV_BIN / "python"), "-m", "pytest", "-q", f"--junitxml={xml}", *args])
    counts = junit(xml) if xml.exists() else {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    return {"layer": name, "ok": code == 0, "seconds": secs, **counts, "tail": out.splitlines()[-1:]}


def main() -> int:
    ART.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []

    code, out, secs = run([str(VENV_BIN / "ruff"), "check", "src", "tests", "scripts"])
    rows.append({"layer": "lint (ruff)", "ok": code == 0, "seconds": secs, "detail": out.splitlines()[-1:]})
    code, out, secs = run([str(VENV_BIN / "mypy")])
    rows.append({"layer": "types (mypy --strict)", "ok": code == 0, "seconds": secs, "detail": out.splitlines()[-1:]})
    rows.append(pytest_layer("unit", ["tests/unit", "tests/wipe"]))
    rows.append(pytest_layer("integration", ["-m", "integration and not heavy", "tests/integration"]))
    if "--heavy" in sys.argv:  # image, edit, music, vocals, video: about an hour
        rows.append(pytest_layer("integration-heavy (media)", ["-m", "integration and heavy", "tests/integration"]))
    code, out, secs = run([str(LAW_HOME / "runtime" / "bin" / "law"), "install"])
    rows.append({"layer": "installer re-run (expect 0 changes)", "ok": code == 0 and "changed 0 item(s)" in out,
                 "seconds": secs, "detail": out.splitlines()[-1:]})

    evals = sorted((ART / "evals").glob("eval-*.json"))
    eval_rows = latest_per_case(evals)
    sources = ", ".join(f"`{e.name}`" for e in evals) or None

    (ART / "report.json").write_text(json.dumps({"layers": rows, "eval": eval_rows}, indent=2))
    write_markdown(rows, eval_rows, sources)
    return 0 if all(r["ok"] for r in rows) else 1


def latest_per_case(files: list[Path]) -> list[dict[str, object]]:
    """Each case's most recent result across runs (`law eval --only` reruns a subset), in golden-set order."""
    latest: dict[str, dict[str, object]] = {}
    for f in files:  # oldest first, so later runs overwrite earlier results
        for row in json.loads(f.read_text()):
            latest[str(row["id"])] = row
    return list(latest.values())


def write_markdown(rows: list[dict[str, object]], eval_rows: list[dict[str, object]], eval_file: str | None) -> None:
    lines = [
        "# Test report",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M')} on {platform.machine()} macOS {platform.mac_ver()[0]} "
        f"by `scripts/test_report.py` from raw results in `~/LAW/outputs/test-artifacts/`. Do not edit by hand.",
        "",
        "## Engineering gates",
        "",
        "| Layer | Result | Tests | Failures | Skipped | Time (s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        tests = r.get("tests", "")
        fails = (int(r.get("failures", 0)) + int(r.get("errors", 0))) if "tests" in r else ""
        lines.append(f"| {r['layer']} | {'PASS' if r['ok'] else 'FAIL'} | {tests} | {fails} | "
                     f"{r.get('skipped', '')} | {r['seconds']} |")
    lines += ["", "## Capability evaluation (golden set, independent judge)", ""]
    if eval_rows:
        passed = sum(bool(e["passed"]) for e in eval_rows)
        lines += [f"Latest result per case from {eval_file}. **{passed}/{len(eval_rows)} passed.**", "",
                  "| Case | Domain | Result | Checks | Judge | Time (s) |", "|---|---|---|---|---|---|"]
        for e in eval_rows:
            checks = e["checks"] if isinstance(e["checks"], dict) else {}
            check_txt = ", ".join(f"{k} {'✓' if v else '✗'}" for k, v in checks.items()) or str(e.get("error") or "")
            judge = f"{e['judge_score']}/5" if e.get("judge_score") is not None else "n/a"
            lines.append(f"| {e['id']} | {e['domain']} | {'PASS' if e['passed'] else 'FAIL'} | {check_txt} | "
                         f"{judge} | {e['seconds']} |")
    else:
        lines.append("No eval run found. Run `law eval` first.")
    (REPO / "docs" / "TEST-REPORT.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
