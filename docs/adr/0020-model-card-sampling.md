# ADR-0020: Every model runs at its model card's sampling
Status: Accepted (2026-10-26)

## Context
Each role in `settings/roles.yaml` had its own temperature, from 0.1 (reviewer) to 0.6. The model cards say
otherwise:
- Qwen3.8-27B: thinking at temperature 1.0, top_p 0.95, top_k 20; with thinking off, 0.7, 0.8, 20 and a presence
  penalty of 1.5. Its published scores (SWE-bench Pro 61.7, Terminal-Bench 2.1 73.0) use the thinking set.
- Qwen3.6-35B-A3B: thinking at 1.0, 0.95, 20 with a presence penalty of 1.5; thinking off as for Qwen3.8.
- Gemma 4 31B: 1.0, 0.95, 64 for every use.

Qwen's cards warn that low temperatures make thinking models repeat themselves and loop. A wire capture (OpenCode
1.18.7 against a local recording server) showed two things:
- OpenCode agents never sent a temperature, so they ran at oMLX's per-model default (already 1.0). They also never
  sent top_p or top_k.
- LAW's direct calls (chat answers, the translator, judge reviews) did send the role's low temperature.

## Decision
- `settings/registry.yaml` holds each model's sampling from its card: a `thinking` set and, where the card gives
  one, an `instruct` set for thinking off. A role's `temperature` applies only to a model without one.
- LAW's direct calls send the card's set for the call's thinking level.
- OpenCode agents send it explicitly. Each per-level config carries the agent's temperature and the model's
  `temperature: true` capability (without that flag OpenCode sends none). It also carries top_p, top_k, min_p and
  the presence penalty as model options, which OpenCode passes through (checked on the wire).
- Not adopted: the Qwen3.6 card's "precise coding" set (temperature 0.6). That model does no coding here.

## Consequences
- Answers, translations and reviews run at the setting the models were tuned and scored at. Reviews are less
  repeatable from run to run, but the judge no longer reasons at a temperature its card advises against.
- Thinking-off projects get the instruct set (0.7 and a presence penalty) instead of oMLX's thinking defaults.
- A new model needs its card's values in the registry; without them it falls back to the role's temperature.
