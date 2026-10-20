"""Real document rendering: Mermaid diagrams, an SVG wireframe and the house template into a PDF."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from law.handlers import proposal
from law.paths import LawPaths
from law.queue.worker import Done

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]
WIREFRAME = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 800">
<rect x="0" y="0" width="1200" height="800" fill="#f2f2f2"/><rect x="0" y="0" width="1200" height="80" fill="#999"/>
<text x="40" y="52" font-size="32">LAN Chat</text><rect x="40" y="700" width="900" height="60" fill="#fff" stroke="#666"/>
</svg>"""


@pytest.mark.skipif(not shutil.which("mmdc") or not shutil.which("typst"), reason="doc tools not installed")
def test_proposal_renders_to_a_pdf(tmp_path: Path) -> None:
    paths = LawPaths(repo=REPO, home=Path.home() / "LAW", user_home=Path.home())  # real puppeteer config
    docs = tmp_path / "docs" / "proposal"
    (docs / "assets").mkdir(parents=True)
    (docs / "assets" / "wireframe-chat.svg").write_text(WIREFRAME)
    (docs / "proposal.md").write_text("""---
title: LAN Chat
subtitle: Private messaging for local networks
prepared-for: Test
author: LAW
date: 20 October 2026
---

# Executive summary

Proceed with the pilot.

| Option | Cost |
|---|---|
| Build | 10 |

: Options

```{.mermaid caption="Container diagram"}
flowchart LR
  U[Browser] --> A[Angular] --> S[Spring Boot]
```

![Wireframe: chat](assets/wireframe-chat.svg){width=80%}
""")

    async def progress(_: str) -> None:
        return None

    ctx: Any = SimpleNamespace(job=SimpleNamespace(payload={"dir": str(tmp_path), "out": "docs/proposal",
                                                            "prompt": "x"}, id="t", parent=None),
                               progress=progress, cancel_event=asyncio.Event())
    rt: Any = SimpleNamespace(paths=paths)
    done = asyncio.run(proposal.make_handlers(rt)["proposal_render"](ctx))
    assert isinstance(done, Done) and done.result["diagrams_failed"] == []
    pdf = Path(done.result["pdf"])
    assert pdf.read_bytes().startswith(b"%PDF") and pdf.stat().st_size > 20_000
    assert (docs / "assets" / "diagram-1.png").stat().st_size > 5_000
