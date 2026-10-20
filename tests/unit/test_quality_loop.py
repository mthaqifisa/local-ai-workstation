"""The draft -> judge -> revise pipeline end to end with a scripted fake LLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from law import registry, roles
from law.handlers.base import Runtime
from law.paths import LawPaths

REPO = Path(__file__).resolve().parents[2]


class FakeLLM:
    def __init__(self, judge_scores: list[int]) -> None:
        self.scores = judge_scores
        self.calls: list[str] = []

    async def chat(self, model: str, messages, **kw) -> str:
        self.calls.append(model)
        if "gemma" in model:
            score = self.scores.pop(0)
            return f'{{"score": {score}, "issues": ["missing risk section"], "summary": "ok"}}'
        if "previous answer" in messages[-1]["content"]:
            return "improved answer with risk section"
        return "first draft"


def runtime(llm: FakeLLM, tmp_path: Path) -> Runtime:
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    return Runtime(paths, registry.load(paths.registry_file), roles.load(paths.settings / "roles.yaml"),
                   llm)  #


@pytest.mark.parametrize("text,score", [
    ('{"score": 4, "issues": ["x"], "summary": "ok"}', 4),
    ("The document is board-ready (Score: 5), technically sound.", 5),
    ("Overall I would rate this 3/5 because the risks section is thin.", 3),
    ("Score - 4 out of five", 4),
])
def test_verdicts_in_json_or_clear_prose(text, score):
    from law.handlers.core import parse_verdict

    v = parse_verdict(text)
    assert v["score"] == score and not v.get("unscored")


def test_a_verdict_without_any_score_is_marked_for_asking_again():
    from law.handlers.core import parse_verdict

    v = parse_verdict("I reviewed the files and they look reasonable.")
    assert v["score"] == 0 and v["unscored"] is True
