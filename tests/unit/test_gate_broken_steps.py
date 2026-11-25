"""LAW's gate notices a check.sh step that never ran, even when the script exits 0."""

from __future__ import annotations

from law.handlers.tasks import broken_gate_steps


def test_unknown_go_subcommand_is_caught() -> None:
    out = "==> code (gofmt, vet, build, test)\ngo gofmt: unknown command\nRun 'go help' for usage.\nok  example.com/x\n"
    assert broken_gate_steps(out) == ["go gofmt: unknown command"]


def test_missing_program_and_bad_flags_are_caught() -> None:
    out = (".law/check.sh: line 7: golangci-lint: command not found\n"
           "unknown shorthand flag: 'x' in -x\nUnknown command: \"lnt\"\n")
    assert len(broken_gate_steps(out)) == 3


def test_a_clean_run_and_ordinary_failures_text_are_not_flagged() -> None:
    out = ("==> build\nverify: OK (14 pages)\n--- PASS: TestDatabaseRefuses/unknown_side (0.00s)\n"
           "ok  example.com/ledger/internal/ledger 0.3s\nentry rejected: unknown account\n")
    assert broken_gate_steps(out) == []


def test_repeats_are_listed_once() -> None:
    assert broken_gate_steps("go gofmt: unknown command\n" * 4) == ["go gofmt: unknown command"]
