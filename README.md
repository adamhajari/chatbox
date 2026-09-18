# Talkbox

A kid-friendly assistant that answers questions within rules the parents set.
This is **Phase 1: the text core**. You type a question as a kid would, and it prints
the answer Talkbox would speak. See `PLAN.md` for the full plan.

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
talkbox chat -v              # also show each pipeline step's decision
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
| `talkbox.toml` | Runtime settings: provider, model, effort, session history, logging, file paths. |
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
question → limits (schedule, daily cap) → classify* → canned reply* → model → output check* → log
```

Steps marked `*` are Phase 2 guardrails. They are stubs that let everything through today,
so for now blocked and redirect topics are enforced only by the system prompt.
With logging on, every step's decision is stored in the log's `steps_json` column.

## Tests

```bash
pytest                 # unit tests; the model is mocked
pytest -m live         # also exercises the real API (skipped without ANTHROPIC_API_KEY)
```
