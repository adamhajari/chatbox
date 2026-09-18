# Talkbox

A kid-friendly assistant that answers questions within rules the parents set.
This is **Phase 2: the text core plus guardrails**. You type a question as a kid would, and
it prints the answer Talkbox would speak. See `PLAN.md` for the full plan.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
```

`.env` is git-ignored. Never commit it.

## Use

```bash
talkbox chat                 # the chat loop; "new" starts a fresh session, Ctrl-D or "quit" exits
talkbox chat -v              # also show each step's decision, reason, and time (ms)
talkbox --policy other.yaml chat   # use a different policy file
talkbox check-policy         # validate the policy file
talkbox show-prompt          # print the system prompt compiled from the policy
talkbox log -n 10            # show the last 10 logged exchanges (when logging is on)
```

Global options (`--config`, `--policy`, `--db`) go before the subcommand.

## Files

| File | What it is |
|---|---|
| `policies/default.yaml` | The parent policy (what it talks about, when, how). |
| `talkbox.toml` | Runtime settings: provider, answering model, guardrail check models, timeouts, session history, logging, file paths. |
| `.env` | Secrets (`ANTHROPIC_API_KEY`). |
| `data/talkbox.db` | SQLite: per-day question counts, plus the exchange log when logging is on. |

## Sessions, logging, and models

- **Sessions:** within one `talkbox chat` run, follow-up questions ("why?") see the earlier
  exchanges, up to `[chat] max_history_exchanges`. Nothing is remembered between runs.
- **Logging** is off by default (`[logging] enabled = false`). With it off, only a count of
  questions per day is stored (for the daily cap); no question or answer text is saved.
  Set it to `true` to record every exchange with its pipeline decisions.
- **Model:** set `model` under `[provider.anthropic]`. For `claude-haiku-4-5`, delete the
  `effort` line; Haiku doesn't accept it.

## Editing the policy

The policy is plain YAML, checked against a strict schema (`src/talkbox/policy.py`).
Unknown or misspelled keys are rejected, so typos don't silently do nothing.

1. Edit `policies/default.yaml`.
2. Bump `policy_version` by one. Every log row records the version plus a content hash, so
   you can tell which rules produced which answer.
3. Run `talkbox check-policy`, then `talkbox show-prompt` to see what the model will be told.

What you can set:

- `household_age`: the age every answer is written for.
- `persona`: `name` and a list of `tone` words.
- `answers`: `max_sentences`, `max_words`, `say_when_unsure`, `ask_follow_up_question`.
- `ai_disclosure.disclosure_reply`: how it explains that it's a computer. Honesty about being
  a computer is always on and can't be turned off.
- `topics.allowed` / `topics.blocked` / `topics.redirect_to_parent`: each topic has an `id`
  (lowercase, underscores; unique), a `label` and a `description`. Redirect topics also carry
  the exact parent-approved `reply`.
- `schedule`: a `timezone` (e.g. `America/New_York`) and one or more `windows`, each with
  `days` (`mon`…`sun`) and `start`/`end` in 24-hour `HH:MM`. Windows can't cross midnight.
- `limits.daily_questions`: questions per day (counted in the policy's timezone; questions
  turned away by the schedule or cap don't count).
- `canned_replies`: the fixed wording for outside-schedule, daily limit, blocked topics, and errors.

## How a question is handled

```
question → limits → ┬ classify ──┐→ canned reply (redirect/refuse) ─────────→ answer
                    └ model answer ┘→ (allow) output check → pass: model answer
                                                          → fail: blocked-topic reply
```

The guardrail layers (numbers from PLAN.md section 4):

| Layer | What it does | Where |
|---|---|---|
| 1. Policy prompt | The policy is compiled into the answering model's system prompt. | `prompt.py` |
| 6. Limits | Schedule and daily cap, checked first. | `limits.py` |
| 3. Input classification | A separate, fast model call sorts the question into `allow`, `redirect` (with the matching `redirect_to_parent` topic) or `refuse` (with the matching `blocked` topic). It sees the last few exchanges, so a follow-up like "can I try that by myself?" is judged in context. | `guardrails.py` |
| 5. Canned replies | `redirect` gets that topic's parent-approved `reply`; `refuse` gets `canned_replies.blocked_topic`. | `guardrails.py` |
| 4. Output check | Before an answer is returned, the whole answer is checked: length (in code, against `max_words` / `max_sentences` plus `length_tolerance`), then a second model call for topic, honesty about being a computer, blocked or parent-topic content, personal information, unsafe suggestions, and age fit. A failed answer is replaced with the blocked-topic reply. | `guardrails.py` |

Both checks read the topics from the policy on every question, so editing the policy
changes their behavior with no code change. Both use structured output (a JSON schema)
through the provider interface.

**Speed.** The classifier and the answer run at the same time; if the classifier says
redirect or refuse, the answer is thrown away unused and the canned reply comes back
right away. On `allow`, the output check runs after the answer is complete.
Typical measured times with `claude-haiku-4-5` for all three: classify ~1.2 s, answer
~1.3 s (in parallel), output check ~1.2 s, so ~2.8 s for an answered question and ~1.1 s
for a redirect or refusal.

**Fail closed.** If the classifier or the output check errors, times out, or returns
something unusable (wrong format, a redirect naming a blocked topic, and so on), the
kid hears `canned_replies.something_went_wrong`, never an unchecked answer.

**Explainable.** Every step records its decision, a short reason, and its time in
milliseconds. `talkbox chat -v` prints them; with logging on they're stored in
`steps_json`. A redirect looks like:

```
   · classify: redirect [1055 ms] death_and_loss: The child is asking about a pet dying...
   · canned: use [0 ms] death_and_loss
   · generate: discarded classifier said redirect
```

### Guardrail settings (`talkbox.toml`)

| Setting | Meaning |
|---|---|
| `[provider.anthropic] timeout_seconds` | Deadline for the answering model. |
| `[guardrails] history_exchanges` | Recent question/answer pairs the checks see. |
| `[guardrails] length_tolerance` | Fraction over the policy's length limits still accepted (0.25 = 25%). |
| `[guardrails.output_check] enabled` | `false` turns layer 4 off entirely, length check included. Saves about 1 s per answered question, but answers are no longer checked before the kid hears them. `talkbox chat` shows "output check OFF" and each answer's steps show `output_check: skipped`. |
| `[guardrails.classifier]` / `[guardrails.output_check]` | Each has `model`, `max_tokens`, `timeout_seconds` (hard deadline; past it the pipeline fails closed) and `max_retries`. They use the same provider as the answering model. |

## Tests

```bash
pytest -m "not live"   # unit tests only; the model is mocked
pytest                 # also runs the live smoke tests when ANTHROPIC_API_KEY is set
pytest -m live -s      # just the live smoke tests, printing each step and its time
```

Unit tests use a frozen copy of the policy (`tests/fixtures/policy.yaml`), so editing
`policies/default.yaml` doesn't break them. The live tests use it too.
