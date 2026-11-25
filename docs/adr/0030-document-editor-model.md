# ADR-0030: Inkling 2 Mini is the document editor (fact-checker)

Date: 2026-11-25. Status: accepted (editor role). The reviewer and QA roles stay on Gemma 4 31B until their own
calibrations pass.

## Context

The editor fact-checks documents section by section (ADR-0029). On the chapter-1 trial it was the slowest and
weakest stage. Gemma 4 31B ran one review round for 6 hours before hitting the job limit. Two measured causes:

- oMLX stopped reusing Gemma's prompt cache above about 23K tokens. Over about 1,000 editor steps, a step below
  22K took about 6 s and one above 24K took 110–130 s.
- It searched badly: about 30 grep variants for one penalty clause. It also stopped with confirmed errors not
  yet written down ("initial evidence gathering").

The owner asked for the best model for reviewing documents, provided it is free and open source and runs well on
this Mac (64 GB; the oMLX guard is 48 GB).

## Options

A survey covered Qwen, Google, NVIDIA, DeepSeek, Moonshot, Microsoft, Mistral, Z.ai, Thinking Machines, Nanbeige
and Prism (Bonsai), up to 25 November 2026. Every stronger model is 100B+ parameters, and those fit only at 2–4 bits.
The shortlist:

- **Gemma 4 31B 8-bit** (current).
- **Nanbeige4.3-33B**: dense, ranked first among 4–40B open models on Artificial Analysis. Converted to 8-bit with
  mlx-lm.
- **Inkling 2 Mini**: a 26.4B looped MoE (3.4B active), ranked second. The vendor's MXFP8/NVFP4 checkpoint needs
  oMLX 0.8.1 or later; oMLX was upgraded to 0.8.3.
- **Qwen3.8-27B** was excluded: it is the writer, so it would review its own family's output.

## Evidence

`scripts/editor_calibration.py` plants seven errors in two chapter-1 sections, each with its truth in the saved
sources:
- a wrong Act year;
- a wrong Act;
- an invented deposit cap;
- RM500m for RM300m;
- RM5bn for RM3bn;
- a 10% TCR for 8%;
- an invented 150-staff rule.

It runs them through LAW's own editor path: the same prompt, OpenCode agent, read limits and environment, with only
the model swapped. When a session stops before its report, the same session is asked to finish it, as LAW now
does.

| Model | Planted errors recorded | Other findings | Time (2 sections) | Decode | First token at 24–56K |
|---|---|---|---|---|---|
| Gemma 4 31B (28K context) | 0 of 7 (3 seen, none written) | 0 | 40 min | 6–7 tok/s | 110–130 s above 23K |
| Nanbeige4.3-33B | stopped after 41 min in section 1 | — | 7–15 min per step | ~7 tok/s, 4.7–6.1K thinking tokens per step | — |
| Inkling 2 Mini (64K context) | **7 of 7**, each quoting the source line | 9: 8 checked as correct, 1 about the test copy itself | 47 min | 31–62 tok/s | median 1–2 s |

Inkling's extra findings were checked by hand against the sources:
- a footnote cites §1.2, which is only the purpose statement; the definition is §5.2;
- the FAQ gives no application-stage figure;
- "deposit-taking member" is defined in s2(1) of the MDIC Act;
- e-money issuers are approved, not licensed.

The first Gemma rerun was stopped once its result could no longer change the decision.

## Decision

- The editor role moves to a new model role, `doc_judge`, served by Inkling 2 Mini at 64K context.
- The reviewer and QA keep the `judge` model (Gemma) until `scripts/judge_calibration.py` and a QA check show
  Inkling is at least as good there.
- The editor's read limits stay: smaller prompts are faster on any model.
- Nanbeige was deleted (an A/B loser).

## Consequences

- Faster and stronger fact-checking. A section's check is bounded by the work, not by cache re-reads.
- Inkling's model card warns of occasional reasoning loops. LAW's repeat-command guard, output cap and
  same-session report completion apply.
- A second heavy model family on disk (20 GB). Gemma stays until the other roles are decided.
