# ADR-0017: Tony's chat: conversation memory, hand-off to agents, fast replies, shared model slots
Status: Accepted (2026-10-24)

## Context
The owner's Telegram conversation of 2026-10-23/24 showed four faults in the default chat (`ask`, role `assistant`):
1. **No memory.** Every message was a fresh conversation: "Proceed with the pdf report" and "Continue" were answered
   with "I don't have any prior context".
2. **Work stuck in chat.** "Study the v6 project, write a BCG-level PDF report with diagrams, tables and a benchmark,
   send it by Telegram, email it, store it in ../doc" went to the chat model, whose only tool is `run_python` with
   six calls. It ran for 24 and 31 minutes, used the calculator to read files and curl the API, ran out of tool
   budget and produced no PDF, no email and no Telegram message. The agents that can do this work (OpenCode, with
   files, shell and the house PDF pipeline) were never involved.
3. **Lag on every message.** "hey bro" took 29 s: the dense Qwen3.8-27B writes ~10 tokens/s on this M5 Pro (measured
   2026-10-24 on the wire: 9.9 tokens/s), and every answer, however short, ran draft (27B) -> judge (Gemma-4-31B,
   a model swap) -> revise stage (another swap). Model loads are 5-11 s each.
4. **Chat queued behind agents.** The heavy lane runs one job at a time (ADR-0009), so a message sent while an agent
   step runs (often an hour or more) waits for the whole step.

## Decision
- **Memory.** Tony keeps the recent conversation per chat in `~/LAW/state/telegram-chats/<chat>.json` (16 messages,
  6,000 characters each, alternating user/assistant): each question and answer, and each agent job's outcome.
  Every chat question carries it as `history`; the draft and the revision see it as chat turns, the judge as text
  context. `/new` starts over.
- **Hand-off.** Roles with the `agents` tool (the assistant) get a `start_agent(role, request, folder)` tool in chat.
  It queues one `task` job (writer, researcher, coder, analyst, designer, architect or operator) with the same
  `reply_to` chat, so Tony follows it there (progress, result, Retry/Skip). The agent's work gets its own pipeline
  (brief, review, fix). One agent per message: a retried model call cannot start a second one. A named folder must be
  inside the user's home folder and is created; without one a non-coder agent gets `~/LAW/outputs/agents/<job>`.
- **Delivery.** `law send <files> [--email [--to]] [--subject] [-m]` sends files to the allow-listed Telegram chats
  (Bot API, the bot's own token) and/or by Mail.app (law.mailer). The house standard tells every agent with a shell
  to use it and to report a file as sent only after its output says so. The writer role gets `shell` so it can build
  PDFs (pandoc + Typst), diagrams (mermaid-cli) and charts (matplotlib via `uv run --with`).
- **Fast chat model.** The assistant role uses `fast` (Qwen3.6-35B-A3B, MoE, 3B active, vision): ~75 tokens/s,
  measured 2026-10-24 (warm "hey bro": 4.5 s at medium thinking vs 8-17 s on the 27B). The agents keep the 27B.
- **Review only where it adds value.** The `ask` job writes the draft itself, then:
  - an answer of at most the role's `quick_reply_chars` (assistant: 700) goes straight back;
  - an answer that handed work to an agent goes straight back (the agent's work is reviewed);
  - an answer written in a shared slot (below) goes straight back, because the reviewer model is not loaded and
    waiting for it would mean waiting for the agent step;
  - everything else runs judge (another model family) -> revise as before. The result says which (`review`).
  Roles other than the assistant keep `quick_reply_chars: 0`: every answer reviewed.
- **Shared heavy slots.** While a heavy job runs on a language model, up to two jobs marked `share_model` (chat
  `ask`/`revise` without images) start beside it on that loaded model; oMLX batches requests on one model (measured:
  a short chat finished in 6.9 s while a 2,000-token answer streamed on the same model). No second model is loaded,
  so memory is unchanged apart from the chat's KV cache. A media model is never shared. When the step a chat joined
  ends first, the chat becomes the running heavy job, so no other model can replace the one it is using.

## Alternatives considered
- **Apple Foundation Models** (macOS 27 ships `/usr/bin/fm`, the Python `apple_fm_sdk`, and a `LanguageModel`
  protocol that can host MLX models): the on-device model has an 8,192-token context, too small for Tony's history
  and tool loop, and the `fm` CLI first needs the owner to accept Apple's licence (`sudo fm license`). Possible
  later as a sub-second router or for tiny tasks (file names, one-line summaries) because it uses no oMLX memory;
  not needed for the faults above.
- **A keyword router to the agents**: brittle with free text ("study", "report", "send"); the model choosing
  `start_agent` with the conversation in view is more accurate and needs no second model.
- **Running the judge in a shared slot on the agent's model**: that model is usually the same family as the draft,
  which breaks the independent-review rule.

## Consequences
- Short and conversational answers come back in seconds, without the reviewer. Long answers keep the review except
  while an agent step is running, where the answer is marked unreviewed in its result.
- Chat answers no longer queue behind agent steps; an agent step running beside a chat slows a little while both
  generate.
- Requests that need files, commands or documents become tracked agent jobs with Retry/Skip, instead of a chat answer
  that runs out of tools.
