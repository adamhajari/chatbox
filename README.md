# Talkbox

A kid-friendly assistant that answers questions within rules the parents set.
This is **Phase 4: parent controls**. Hold the spacebar, ask a question out loud, and
Talkbox speaks the checked answer. A parent can change the rules from a phone while it runs.
The typed chat from Phases 1–2 still works. See `PLAN.md` for the full plan.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
```

`.env` is git-ignored. Never commit it.

### Voice setup (for `talkbox talk`)

Speech goes through Google Cloud Speech-to-Text v2 and Text-to-Speech (PLAN.md D19).
Google doesn't log the audio unless you opt in, so leave its "data logging" off. Talkbox
never writes audio to disk.

1. At console.cloud.google.com, create a project, link billing, and set a budget alert
   (Billing → Budgets & alerts; about $10/month is plenty).
2. Enable the **Cloud Speech-to-Text API** and the **Cloud Text-to-Speech API**.
3. IAM & Admin → Service Accounts → create `talkbox-speech` with the **Cloud Speech Client**
   role. Open it → **Keys** → **Add key** → **Create new key** → JSON.
4. Move the key outside the repo and lock it down:
   `mkdir -p ~/.config/talkbox && mv ~/Downloads/<key>.json ~/.config/talkbox/gcp-speech.json && chmod 600 ~/.config/talkbox/gcp-speech.json`
5. Add to `.env`: `GOOGLE_APPLICATION_CREDENTIALS=<that path>` and `GOOGLE_CLOUD_PROJECT=<project id>`.

**Audio devices.** Talkbox uses the system's default mic and speaker. To choose others, run
`python -m sounddevice` to list devices, then set `input_device` / `output_device` under
`[voice]` in `talkbox.toml` (a name or a number). On the Pi, install PortAudio first:
`sudo apt install libportaudio2`.

**macOS microphone permission.** The first time `talkbox talk` records, macOS asks whether
your terminal app (Terminal, iTerm, or VS Code) may use the microphone. Allow it. If you
missed the prompt: System Settings → Privacy & Security → Microphone → turn on your terminal
app, then quit and reopen it. Without permission, recordings come back silent and Talkbox
says its "didn't catch that" reply every time.

## Use

```bash
talkbox chat                 # the chat loop; "new" starts a fresh session, Ctrl-D or "quit" exits
talkbox chat -v              # also show each step's decision, reason, and time (ms)
talkbox talk                 # voice: hold SPACE to talk, let go to send; n = new session, q = quit
talkbox talk -v              # also show each step, including speech-to-text and text-to-speech
talkbox talk --web           # also serve the parent settings page (works with chat too)
talkbox --policy other.yaml chat   # use a different policy file
talkbox check-policy         # validate the policy file
talkbox show-prompt          # print the system prompt compiled from the policy
                             # (includes the screen section if this machine has one)
talkbox log -n 10            # show the last 10 logged exchanges (when logging is on)
```

Global options (`--config`, `--policy`, `--db`) go before the subcommand.

To run Talkbox on a Raspberry Pi instead of the laptop, see
[docs/pi-setup.md](docs/pi-setup.md) — flashing the card through to measuring the board.

## Files

| File | What it is |
|---|---|
| `policies/default.yaml` | The parent policy (what it talks about, when, how). |
| `talkbox.toml` | Runtime settings: provider, answering model, guardrail check models, timeouts, session history, logging, file paths, and the settings page's `[web]` host and port. |
| `talkbox.local.toml` | Per-machine settings (audio devices, GPIO pins), merged over `talkbox.toml`. Never committed; copy `talkbox.local.toml.example` to start. |
| `.env` | Secrets (`ANTHROPIC_API_KEY`, and for voice `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`). |
| `scripts/measure.py` | Times `talkbox chat` end to end, per pipeline step, for comparing machines. |
| `scripts/gpio_check.py`, `scripts/screen_check.py` | Prove the Pi's button, LED and screen are wired right, on their own (see `docs/hardware.md`). |
| `scripts/screen_demo.py` | Shows a real question's picture on the screen, with no microphone or speaker needed. |
| `data/pictures/` | Cached screen pictures, one per subject. Safe to delete; it refills itself. |
| `data/talkbox.db` | SQLite: per-day question counts, the pause switch, plus the exchange log when logging is on. |

## Sessions, logging, and models

- **Sessions:** within one `talkbox chat` run, follow-up questions ("why?") see the earlier
  exchanges, up to `[chat] max_history_exchanges`. Nothing is remembered between runs.
- **Logging** is off by default (`[logging] enabled = false`). With it off, only a count of
  questions per day is stored (for the daily cap); no question or answer text is saved.
  Set it to `true` to record every exchange with its pipeline decisions.
- **Model:** set `model` under `[provider.anthropic]`. For `claude-haiku-4-5`, delete the
  `effort` line; Haiku doesn't accept it.

## Parent settings page

```bash
talkbox talk --web      # or: talkbox chat --web
```

It prints the address to open, for example:

```
Parent settings: open http://192.168.1.20:8321/ on a phone or computer on this network.
```

Open that on any phone or computer on the home Wi-Fi. The page covers the whole policy:
name and tone, answer style, household age, the three topic lists (add, edit, remove, or
move a topic between lists by changing its "List"), each ask-a-parent topic's reply, the
schedule and timezone, the daily limit, and every fixed reply. Honesty about being a
computer is shown but can't be switched off.

- **Right now** (top of the page) shows how many questions have been asked today against
  the daily limit, with two buttons:
  - **Reset today's count** starts today's count over at 0, so a kid who hit the limit
    can keep asking.
  - **Pause Talkbox** makes every question get "I'm currently resting. Let's talk later."
    until you press **Resume**. It takes effect from the next question, beats the schedule
    and limit, and paused questions don't count. The pause is stored in `data/talkbox.db`,
    so restarting Talkbox doesn't undo it (it says PAUSED at startup).

  These buttons act at once and don't touch the policy file or any unsaved edits below.
  The count refreshes by itself every 5 seconds (the page's only bit of JavaScript), so
  unsaved edits are never lost to a reload.
- **Today's questions and answers** (collapsed, at the bottom) lists today's exchanges,
  newest first, with the time and whether it was a model answer or a fixed reply. It only
  has something to show when `[logging] enabled = true` in `talkbox.toml` (off by default,
  PLAN.md D8); otherwise it says logging is off. Reload the page to see new ones.
- **Save** checks the whole policy first. If anything is wrong (an empty reply, a window
  that ends before it starts, two topics with the same id), nothing is saved and each
  problem is shown in red next to its field. `policy_version` goes up by one on every save.
- **Preview prompt** shows the system prompt your unsaved changes would produce. The page
  always shows the version in use and, at the bottom, the system prompt in use now.
- If two people edit at once, the second save is refused with a message rather than
  silently undoing the first one's changes.

**How live updates work.** The settings page runs inside the Talkbox process, so it shares
Talkbox's in-memory policy. A save (1) checks the edit, (2) writes `policies/default.yaml`
by writing a temporary file and renaming it over the old one, so a crash can't leave a
half-written file, and (3) switches the in-memory policy. The next question uses the new
policy everywhere: system prompt, classifier and output check, schedule and daily limit,
and fixed replies. A question already being answered finishes under the policy it started
with. The file stays the durable copy: Talkbox reads it at startup. There is no separate
`talkbox serve`, because a second process would change the file without the running
Talkbox knowing. Without `--web`, edit the file and restart.

**Address.** `[web] host = "auto"` (the default) listens only on this computer's
home-network address, not on every network interface; `port` defaults to 8321. Set `host`
to a specific address, or `"127.0.0.1"` for this computer only. Requests from outside
private network ranges, and forms posted from other websites, are refused.

> **No password yet (PLAN.md D7a).** Anyone on the home network can open the page and
> change what Talkbox will talk about, including kids and guests. That's acceptable while
> only adults are testing. **Add a password before the kid pilot (Phase 5)**: kids share
> the Wi-Fi and could unblock topics or lift the daily limit.

**What happens to the file's comments.** A save rewrites `policies/default.yaml` from the
saved policy. The comment block at the top of the file is kept; any other comments, blank
lines, and YAML styles (such as `>-` for long text) are lost. The guidance that used to
live there is in this README and in the help text on the page.

## Editing the policy file

The policy is plain YAML, checked against a strict schema (`src/talkbox/policy.py`).
Unknown or misspelled keys are rejected, so typos don't silently do nothing. You can
still edit it by hand (with Talkbox stopped, or restart it afterwards):

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
- `canned_replies`: the fixed wording for outside-schedule, daily limit, blocked topics, errors, and (voice) `didnt_catch_that` when nothing was heard. Added in `schema_version: 2`; a version-1 policy needs that line added and its `schema_version` set to 2.

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

## Voice

`talkbox talk` runs pipeline B (PLAN.md D3):

```
hold SPACE ─▶ mic streams to speech-to-text ─▶ release ─▶ transcript
   ─▶ the text pipeline above (limits, classifier, answer, output check), unchanged
   ─▶ final checked text ─▶ text-to-speech, streamed ─▶ speaker
```

- **Guardrails stay in front of the speaker.** Nothing is sent to text-to-speech until the
  text pipeline has returned its final text, after the output check (when it's on).
  Canned replies (redirects, refusals, limits) are spoken the same way.
- **Streaming both ways.** Audio goes to speech-to-text while the kid is still talking, so
  the transcript is ready moments after release. Playback starts on the first chunk of
  synthesized audio.
- **Always audio feedback** (neither kid reads yet): a rising beep when listening starts, a
  falling beep on release, a soft blip for a tap that was too short (nothing is sent), the
  policy's `didnt_catch_that` reply for silence or no recognized words, the policy's
  `something_went_wrong` reply when speech-to-text fails, and a low two-note error sound if
  text-to-speech itself fails.
- **Push-to-talk in a terminal.** Terminals report key presses but not releases. Holding
  a key makes macOS repeat it, so Talkbox treats "repeats stopped" as the release. If
  releases feel late or taps count as holds, tune `key_repeat_wait_seconds` and
  `release_gap_seconds`, or the keyboard repeat settings in System Settings → Keyboard.
  If key repeat is turned off entirely, every press looks like a tap.
- **Adapters.** The voice turn (`src/talkbox/voice.py`) never touches devices. The laptop
  adapter (`src/talkbox/audio/laptop.py`) supplies the mic, speaker and spacebar. The Pi's
  (`src/talkbox/audio/pi.py`) adds the arcade button, the status LED and the screen, each
  behind a wrapper so the voice turn stays free of hardware.
- **Swappable services.** `SpeechToText` and `TextToSpeech` in `src/talkbox/speech/base.py`.
  To add a vendor, add a class and a name in `make_stt` / `make_tts`.

### Speech settings (`talkbox.toml`)

| Setting | Meaning |
|---|---|
| `[speech] language` | Language for both services (`en-US`). |
| `[speech.stt] name`, `[speech.tts] name` | Which vendor (`google`). |
| `[speech.stt.google] model`, `location` | Recognition model and its region. Measured time from release to transcript: `long` / `global` ≈ 0.13 s; `chirp_3` / `us` ≈ 0.75 s; `chirp_2` / `us-central1` ≈ 0.8 s. The Chirp models are newer and may cope better with young kids' speech; compare them on the kids' voices during the pilot. |
| `[speech.tts.google] voice` | Any Chirp 3 HD voice name, e.g. `en-US-Chirp3-HD-Leda`. |
| `[voice] min_press_seconds` | Shorter presses count as accidental taps. |
| `[voice] silence_rms` | Recordings quieter than this get "didn't catch that" without calling speech-to-text. |
| `[voice] stt_timeout_seconds` | Deadline for the transcript after release; past it, the kid hears `something_went_wrong`. |
| `[voice] max_seconds` | Longest recording. |
| `[voice] key_repeat_wait_seconds`, `release_gap_seconds` | Spacebar hold/release detection (see above). |
| `[voice] input_device`, `output_device`, `sample_rate` | Audio devices and mic sample rate. |

`talkbox talk -v` shows the steps: `stt` (time from release to transcript), each text
pipeline step, `pipeline` (total), `tts` (time to first audio), and `speech_start` (end of
question to first audio, the D4 number: at most 5,000 ms).

## The screen (Pi only)

A 2.2" SPI display showing a picture of what was asked about while Talkbox answers
(PLAN.md D28/D29). **Pictures only, never text** — neither kid reads fluently, so the
screen supplements the spoken answer and is never needed to understand it.

```
classifier (already running) ─▶ subject ─▶ Wikipedia article's lead image ─▶ cache
                                                                             │
        answer starts playing ─▶ show whatever is ready ─── turn ends ─▶ blank
```

- **The subject comes from the guardrail classifier**, which already runs on every
  question, so there is no extra model call and no extra latency. `Classification.subject`
  is the concrete thing the question is about (`"octopus"`, `"the Moon"`), or `None`
  where there isn't one ("why do we have to sleep?"). It never affects a decision.
- **Pip knows the screen exists.** With it enabled, the system prompt gains a section
  telling it not to deny that it can show things — without it, "can you show me a
  picture of an octopus?" got "I can't show you pictures, but I can tell you about
  them". It still never says what the picture is or promises one, and every answer has
  to make complete sense with the screen off. `talkbox show-prompt` shows the section
  when this machine has a screen; the honesty rules are untouched either way.
- **Landscape, 320x240.** The panel is 240x320 glass turned 90 degrees (`[screen]
  rotation`), because a photograph of a thing is usually wider than it is tall.
  Pictures are letterboxed to that shape on black, never stretched.
- **The picture is the Wikipedia article's lead image** for that subject — not a
  Wikimedia Commons free-text image search, which ranks every file anyone has ever
  uploaded against a word. One API request per subject, then a disk cache, including a
  negative entry so a subject with no picture is looked up once.
- **Speech never waits for a picture.** The lookup runs on its own thread with a hard
  timeout (`[screen] timeout_seconds`). Whatever is ready when the answer starts is
  shown; anything still in flight appears late, or not at all. D4's 5 s budget is
  untouched.
- **Nothing can break a turn.** No article, no network, a dead panel, a missing library:
  every one of them means no picture and an identical spoken answer.
- **Blocked and redirected questions show nothing.** Only an "allow" starts a lookup.
- **Between turns the screen is blank**, and with `backlight_gpio` set the backlight
  goes out with it, so it's genuinely dark rather than a lit grey rectangle.
- **`talkbox chat` uses it too**, not just `talkbox talk`: the picture goes up with the
  printed answer and comes down when the next question is typed. That is the only way
  to see a whole turn on a Pi whose microphone and speaker aren't wired yet. It appears
  a moment *after* the text, because the lookup only starts when the classifier returns
  and there is no speech to cover it.
- **Adapters, not plumbing.** `talkbox/voice.py` and the pipeline know nothing about the
  screen, exactly as with the status light. `talkbox/audio/picture.py` wraps the
  classifier (to catch the subject) and the speaker (to show and clear); the panel itself
  is `Screen` in `talkbox/audio/pi.py`, lazily imported.

> **The picture is not checked by any guardrail before a child sees it** (PLAN.md D29).
> The classifier checks the *question*; nothing checks the image that comes back. This
> is a real gap in the promise that nothing reaches a child unchecked, accepted for v1
> only because Talkbox is family-only and supervised (D17). It must be revisited before
> unsupervised use, and certainly before any other family's children — most likely as a
> parent-approved subject list edited from the settings page.

### Screen settings (`talkbox.toml`)

The screen is off unless a machine turns it on, so these live in `talkbox.local.toml`
on the Pi; `talkbox.toml` documents them and ships with `enabled = false`.

| Setting | Meaning |
|---|---|
| `[screen] enabled` | `false`, or no `[screen]` section, means no screen: nothing imported, no pin opened, no picture fetched. |
| `[screen] dc_gpio`, `reset_gpio`, `cs` | D/C and RESET pins, and which SPI0 chip select (0 = CE0). |
| `[screen] baudrate` | SPI clock. Drop to `16000000` if the picture is noisy or torn. |
| `[screen] rotation` | `0`, `90`, `180` or `270`, if the panel is mounted the other way up. |
| `[screen] backlight_gpio` | The backlight's pin, so a blank screen is genuinely dark. Unset = wired to 3V3 and always on. |
| `[screen] backlight_active_high` | `false` if a P-MOSFET switches the backlight (the pin lights it by going low). |
| `[screen] timeout_seconds` | Hard ceiling on looking a picture up. Speech never waits for it. |
| `[screen] cache_dir` | Where pictures are cached. Unset = `pictures/` beside the database. |

Wiring, the parts list and what each failure looks like: `docs/hardware.md`.
`scripts/screen_check.py` proves the panel on its own before any of this is turned on.

## Tests

```bash
pytest -m "not live"   # unit tests only; the model, speech services and audio devices are mocked
                       # (includes the settings page, via FastAPI's test client)
pytest                 # also runs the live smoke tests when ANTHROPIC_API_KEY is set
pytest -m live -s      # just the live smoke tests, printing each step and its time
                       # (the voice one needs the Google keys; it plays tests/fixtures/question.wav,
                       #  a synthetic voice, through real speech-to-text and text-to-speech.
                       #  The screen's live test calls the real Wikipedia API and needs no key.)
```

Unit tests use a frozen copy of the policy (`tests/fixtures/policy.yaml`), so editing
`policies/default.yaml` doesn't break them. The live tests use it too.
