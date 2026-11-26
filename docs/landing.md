<!-- The top of docs/index.html (scripts/build_docs.py). Every figure here is measured and dated, with its record. -->

```{=html}
<section class="hero">
  <p class="eyebrow">Open source · Apple Silicon · One Mac, your whole network</p>
  <h1 class="hero-title">LAW: a private AI team<br>on a single Mac</h1>
  <p class="hero-lede">LAW turns a 64 GB Apple Silicon Mac into an AI workstation for everyone on your network.
    A team of agents plans, builds and tests software end to end, answers and researches with sources, writes
    proposals and reports, operates apps on your Mac, and makes images, video, speech and music, all on open
    models that run on the Mac itself. Before you see any answer, a model of a different family has checked it.</p>
  <div class="hero-actions">
    <a class="button primary" href="#get-started">Get started</a>
    <a class="button" href="#screenshots">See it working</a>
    <a class="button" href="#overview">Read the documentation</a>
    <a class="button github" href="https://github.com/mthaqifisa/local-ai-workstation/tree/v6-simplified-mlx"><svg aria-hidden="true" viewBox="0 0 16 16" width="18" height="18"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>View on GitHub</a>
  </div>
  <p class="repo">Source: <a href="https://github.com/mthaqifisa/local-ai-workstation/tree/v6-simplified-mlx">github.com/mthaqifisa/local-ai-workstation</a> · free and open source</p>
</section>
```

# Why LAW {#why-law}

Cloud AI is fast to start and hard to trust with your work: your data leaves the building, the bill grows with
use, and the model that writes an answer is usually the only one that checks it. LAW is built the other way round.

```{=html}
<div class="grid">
  <article class="card"><h3>Private by design</h3><p>Models run on the Mac. Documents, code and conversations stay
    on your network. Secrets live in one file outside the repository, and git hooks block personal data from ever
    being committed.</p></article>
  <article class="card"><h3>Never grades its own work</h3><p>Every answer goes through draft, independent review and
    revision. The reviewer is a different model family, so one model's blind spots are not approved by the same
    model. Numbers are computed in code, never guessed.</p></article>
  <article class="card"><h3>Free to run</h3><p>Open models and open-source tools only: no licence fees, no
    subscriptions, no per-token bill. Every model and tool is pinned to an exact version, so an install today
    matches an install next year.</p></article>
</div>
```

# Proof, not promises {#proof}

Each figure below was measured on the Mac LAW runs on, and each links to the record that shows how.

```{=html}
<div class="facts">
  <div class="fact"><span class="num">2.6–2.8×</span><span class="label">faster code generation from speculative
    decoding on the main model, with identical tool calls</span><span class="src">measured 2026-11-23 ·
    <a href="adr/0025-speculative-decoding-dflash.md">ADR-0025</a></span></div>
  <div class="fact"><span class="num">10 / 10</span><span class="label">golden evaluation cases passed through the
    real product, scored by an independent judge</span><span class="src">2026-11-23 · <code>law eval</code></span></div>
  <div class="fact"><span class="num">7 of 7</span><span class="label">planted errors caught by the independent
    reviewer of documents, each with the source line that disproves it</span><span class="src">2026-11-25 ·
    <a href="adr/0030-document-editor-model.md">ADR-0030</a></span></div>
  <div class="fact"><span class="num">754</span><span class="label">automated tests, with lint and strict type
    checks on every change</span><span class="src">2026-11-26 · pytest, ruff, mypy --strict</span></div>
  <div class="fact"><span class="num">32</span><span class="label">architecture decision records: every choice,
    the options compared and the evidence</span><span class="src"><a href="adr">docs/adr</a></span></div>
  <div class="fact"><span class="num">0</span><span class="label">changes when the installer runs a second time: it
    only fixes what drifted</span><span class="src"><code>law install</code> · re-run guarantee</span></div>
</div>
```

# What it does {#capabilities}

```{=html}
<div class="grid">
  <article class="card"><h3>Software, end to end</h3><p>Describe a product on Telegram. Agents write a proposal
    and a plan you approve, build it ticket by ticket, test the running product as a black box every round, fix
    what QA finds and hand over a document with real screenshots. After every ticket LAW runs the project's own
    build and tests itself: the agent's word is never taken for it.</p></article>
  <article class="card"><h3>A coding agent on call</h3><p><code>/code</code> or <code>law code</code> works in any
    project folder: new features, fixes and refactors, kept honest by the project's build, lint and tests.
    <code>/bugfix</code> reports a bug; the coder fixes it and QA retests the product.</p></article>
  <article class="card"><h3>Answers and research with sources</h3><p>Chat in Telegram, Open WebUI or the terminal,
    with private web search, your documents and knowledge bases, and a calculator the model must use for any
    figure. Replies show the model's live reasoning.</p></article>
  <article class="card"><h3>Proposals, reports and books</h3><p><code>/proposal</code> researches and writes a
    consultant-grade proposal as a PDF. <code>/rnd</code> writes reports, research papers and books chapter by
    chapter: every fact quoted from a saved source, code samples that compile, and an independent editor.</p></article>
  <article class="card"><h3>An agent that uses your Mac</h3><p>The operator opens apps, plays music, changes the
    volume and settings, runs Shortcuts and takes screenshots to check its own work: "play calm jazz on Spotify at
    25% volume" just happens.</p></article>
  <article class="card"><h3>Images, video, speech and music</h3><p>Generate and edit images, animate a still, make
    a short film with planned shots, a score and cuts, compose full songs with lyrics, speak text in natural voices
    and transcribe recordings.</p></article>
  <article class="card"><h3>A team of specialists</h3><p>Architect, analyst, designer, researcher, writer,
    translator, coder, reviewer and QA: each role has its own instructions, tools and model, and you can call any
    of them directly with <code>/agent</code>.</p></article>
  <article class="card"><h3>Your Mac as an AI server</h3><p>OpenAI- and Anthropic-compatible APIs for VS Code,
    Claude Code, scripts and apps on every device on your network, and it keeps working without internet once the
    models are downloaded.</p></article>
  <article class="card"><h3>You stay in charge</h3><p>Plans and key changes wait for your Go on Telegram. When an
    agent is stuck, a lead developer drafts a note and you approve, edit or drop it. <code>/note</code> steers a
    running agent at its next step; the Console shows every agent live.</p></article>
</div>
```

# How it works {#how-it-works}

```{=html}
<ol class="flow">
  <li><strong>You ask</strong><span>From Telegram, the terminal, a browser or the API.</span></li>
  <li><strong>LAW schedules</strong><span>One durable queue; the scheduler loads the model that fits in memory.</span></li>
  <li><strong>An agent works</strong><span>Research, writing, code or media, saved as it goes, so nothing is lost.</span></li>
  <li><strong>Checks run</strong><span>The project's own build and tests, then a reviewer of another model family.</span></li>
  <li><strong>You decide</strong><span>Plans, notes and final results come to you for a Go.</span></li>
</ol>
```

A single Mac holds one large model at a time, so the scheduler swaps 30–45 GB models in and out as jobs need them:
a reasoning model with speculative decoding for planning, writing and code, a fast coder, a fast assistant and
operator, an independent judge, a document editor, and media models for images, video, speech and music. The full picture, with every
component and port, is in [Architecture](#architecture).

# Screenshots {#screenshots}

```{=html}
<p class="lede">The LAW Console runs at <code>http://127.0.0.1:8100</code> on the Mac (and at the Mac's address on
your network). Every tab, as it looks on a working day:</p>
<div class="gallery">
  <figure class="wide"><img src="docs/images/screenshots/console-overview.png" alt="Console overview: CPU, memory, GPU, storage, models in memory and the queue">
    <figcaption><strong>Overview.</strong> CPU, memory, GPU and storage of the Mac, the models in memory and the job
    queue, live.</figcaption></figure>
  <figure class="wide"><img src="docs/images/screenshots/console-agents.png" alt="Agents: the model's current generation, the running agent's step, last action and progress">
    <figcaption><strong>Agents.</strong> What the model is generating, each agent's current step, its last action and
    how far it is through its plan, with a Cancel button.</figcaption></figure>
  <figure class="wide"><img src="docs/images/screenshots/console-projects.png" alt="Projects: a project board with To do, In progress, Waiting for approval, Completed and Failed columns">
    <figcaption><strong>Projects.</strong> Every project, software or document, as a board of tickets, with estimates
    that recalibrate to the real pace and a live countdown on the running ticket.</figcaption></figure>
  <figure><img src="docs/images/screenshots/console-services.png" alt="Services: every LAW service with its live response time">
    <figcaption><strong>Services.</strong> Chat, the image studio, web search, the LLM endpoint, the job API and
    Docker, each with its live response time.</figcaption></figure>
  <figure><img src="docs/images/screenshots/console-jobs.png" alt="Jobs: running jobs with status, progress and Cancel">
    <figcaption><strong>Jobs.</strong> Every job in the queue with its status and latest progress; cancel any of
    them.</figcaption></figure>
  <figure><img src="docs/images/screenshots/console-storage.png" alt="Storage: every cache with its size, what clearing it costs and a Clear button">
    <figcaption><strong>Storage.</strong> Every cache with its size and what clearing it costs, cleared with one
    button.</figcaption></figure>
  <figure><img src="docs/images/screenshots/console-personas.png" alt="Personas: the agent roles and the coder role's editable instructions">
    <figcaption><strong>Personas.</strong> Each role's instructions, model, reasoning and tools, editable; saving
    regenerates the agents.</figcaption></figure>
  <figure><img src="docs/images/screenshots/console-settings.png" alt="Settings: the Telegram bot, allowed users and the report email">
    <figcaption><strong>Settings.</strong> The Telegram bot and who may use it, and where finished documents are
    emailed. Secrets are stored only in <code>~/LAW/state/.env</code>.</figcaption></figure>
</div>

<h3 class="gallery-title">One example of agent output: a technical book with working Go and Java code</h3>
<div class="gallery pages">
  <figure><img src="docs/images/screenshots/book-data-model.png" alt="A book page with an entity-relationship diagram and the SQL that creates it">
    <figcaption><strong>The data model.</strong> Diagram and database schema, both from the same agent-built
    project.</figcaption></figure>
  <figure><img src="docs/images/screenshots/book-go-code.png" alt="A book page of Go source code with explanatory comments">
    <figcaption><strong>Code that compiles.</strong> Every sample builds and its tests pass on the stated versions
    before the chapter is accepted.</figcaption></figure>
  <figure><img src="docs/images/screenshots/book-holds-table.png" alt="A book page with a worked table of account balances and cited footnotes">
    <figcaption><strong>Cited, worked examples.</strong> Tables trace real numbers; every claim carries a footnote
    with a link.</figcaption></figure>
  <figure><img src="docs/images/screenshots/book-eod-diagrams.png" alt="A book page with two sequence diagrams of an end-of-day process">
    <figcaption><strong>Diagrams that fit the page.</strong> Rendered from source, checked for readable text and
    page fit.</figcaption></figure>
</div>
```

# Get started {#get-started}

```{=html}
<pre class="start"><code>git clone -b v6-simplified-mlx https://github.com/mthaqifisa/local-ai-workstation.git ~/SourceCode/OpenSourceLocalAI/v6
cd ~/SourceCode/OpenSourceLocalAI/v6
./install.sh        # safe to run again: it only changes what is out of date
law doctor          # checks every service and prints the addresses on your network</code></pre>
```

You need an Apple Silicon Mac with 64 GB of memory, Homebrew and Python, and room for the models: the first
install downloads about 200 GB, and a Mac with every optional model holds about 300 GB. The
[Quick start](#quick-start) covers the first ten minutes, the [Runbook](#runbook) covers updates and
troubleshooting, and [Architecture](#architecture) explains every component. The rest of this page is the full
documentation.
