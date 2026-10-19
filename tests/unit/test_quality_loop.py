"""The draft -> judge -> revise pipeline end to end with a scripted fake LLM."""

from __future__ import annotations

from pathlib import Path

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
