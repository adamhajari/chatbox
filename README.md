# Chatbox

A voice assistant for young children that answers questions within rules their parents
set. A child holds a button, asks a question out loud, and Chatbox speaks an answer
written for their age, after checking both the question and the answer against the
parents' rules. Parents change those rules (topics, hours, a daily limit) from a phone or laptop
while it runs.

It runs on a Raspberry Pi with a button, a status light, a speaker, a microphone and an
optional picture screen ([docs/hardware.md](docs/hardware.md)). It also runs on a laptop,
where the spacebar is the button, and as a typed chat. Answers come from Claude; speech
goes through Google Cloud.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env        # then put your OPENAI_API_KEY (or ANTHROPIC_API_KEY) in .env
```

`.env` is git-ignored. Never commit it.

### Model API key

Chatbox's answers and its safety checks come from one model vendor, chosen by
`[provider] name` in `chatbox.toml`: `openai` (the default, `gpt-6-luna`) or `anthropic`
(Claude, `claude-haiku-4-5`). You need a key for the vendor you choose. Every question
costs a small fraction of a cent.

**OpenAI:** at [platform.openai.com](https://platform.openai.com/), add credits and set a
monthly budget under **Limits**, create a key under **API keys**, and put it in `.env` as
`OPENAI_API_KEY=sk-...`. The same key covers OpenAI text-to-speech.

**Anthropic:**

1. Go to [platform.claude.com](https://platform.claude.com/) and sign in, or create an
   account.
2. Add credits under **Settings → Billing**. API calls fail until the account has a
   balance. While you're in settings, set a monthly spend limit so a runaway loop can't
   run up a bill.
3. Go to **Settings → API keys** and click **Create key**. Name it (e.g. `chatbox`), set
   **Linked account** to yourself, and choose an expiration. A key that expires stops
   Chatbox on that date, so pick one you'll remember to renew, or no expiration.
4. **Copy the key now.** It starts with `sk-ant-` and the Console shows it only once. If
   you lose it, create a new one.
5. Put it in `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

Check it works with `chatbox chat`: ask a question and you should get an answer. A
`401`, `Could not resolve authentication method` or `api_key client option must be set`
error means the key in `.env` for the chosen vendor is missing or mistyped.

### Voice setup (for `chatbox talk`)

Speech-to-text goes through Google Cloud Speech-to-Text v2. Text-to-speech goes through
Google Cloud Text-to-Speech (Neural2-J by default), or OpenAI (`gpt-4o-mini-tts`) if you
set `[speech.tts] name = "openai"`.
Google doesn't log the audio unless you opt in, so leave its "data logging" off. Chatbox
never writes audio to disk.

1. At console.cloud.google.com, create a project, link billing, and set a budget alert
   (Billing → Budgets & alerts; about $10/month is plenty).
2. Enable the **Cloud Speech-to-Text API** and the **Cloud Text-to-Speech API**.
3. IAM & Admin → Service Accounts → create `chatbox-speech` with the **Cloud Speech Client**
   role. Open it → **Keys** → **Add key** → **Create new key** → JSON.
4. Move the key outside the repo and lock it down:
   `mkdir -p ~/.config/chatbox && mv ~/Downloads/<key>.json ~/.config/chatbox/gcp-speech.json && chmod 600 ~/.config/chatbox/gcp-speech.json`
5. Add to `.env`: `GOOGLE_APPLICATION_CREDENTIALS=<that path>` and `GOOGLE_CLOUD_PROJECT=<project id>`.
6. Only for OpenAI text-to-speech: create an API key at platform.openai.com → API keys, set a
   monthly budget under Limits, and add `OPENAI_API_KEY=<key>` to `.env`.

**Audio devices.** Chatbox uses the system's default mic and speaker. To choose others, run
`python -m sounddevice` to list devices, then set `input_device` / `output_device` under
`[voice]` in `chatbox.toml` (a name or a number). On the Pi, install PortAudio first:
`sudo apt install libportaudio2`.

**macOS microphone permission.** The first time `chatbox talk` records, macOS asks whether
your terminal app (Terminal, iTerm, or VS Code) may use the microphone. Allow it. If you
missed the prompt: System Settings → Privacy & Security → Microphone → turn on your terminal
app, then quit and reopen it. Without permission, recordings come back silent and Chatbox
says its "didn't catch that" reply every time.

## Use

```bash
chatbox chat                 # the chat loop; "new" starts a fresh session, Ctrl-D or "quit" exits
chatbox chat -v              # also show each step's decision, reason, and time (ms)
chatbox talk                 # voice: hold SPACE to talk, let go to send; n = new session, q = quit
chatbox talk -v              # also show each step, including speech-to-text and text-to-speech
chatbox talk --web           # also serve the parent settings page (works with chat too)
chatbox --policy other.yaml chat   # use a different policy file
chatbox check-policy         # validate the policy file
chatbox show-prompt          # print the system prompt compiled from the policy
chatbox log -n 10            # show the last 10 logged exchanges (when logging is on)
```

Global options (`--config`, `--policy`, `--db`) go before the subcommand.

To run Chatbox on a Raspberry Pi, see [docs/pi-setup.md](docs/pi-setup.md), from a
blank SD card to talking to it. To work on Chatbox, developing on a Mac and running it
on a Pi, see [docs/development.md](docs/development.md).

## Files

| File | What it is |
|---|---|
| `policies/local.yaml` | This machine's parent policy (what it talks about, when, how): the one Chatbox uses and the settings page edits. Not tracked by git; created from `policies/default.yaml` the first time Chatbox runs. |
| `policies/default.yaml` | The tracked starting policy. Changing it doesn't affect a machine that already has `local.yaml`. |
| `chatbox.toml` | Runtime settings: provider, answering model, guardrail check models, timeouts, session history, logging, file paths, and the settings page's `[web]` host and port. |
| `chatbox.local.toml` | Per-machine settings (audio devices, GPIO pins), merged over `chatbox.toml`. Never committed; copy `chatbox.local.toml.example` to start. |
| `.env` | Secrets (`OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`, and for voice `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`). |
| `scripts/measure.py` | Times `chatbox chat` end to end, per pipeline step, for comparing machines. |
| `scripts/gpio_check.py`, `scripts/screen_check.py` | Prove the Pi's button, LED and screen are wired right, on their own (see `docs/hardware.md`). |
| `scripts/screen_demo.py` | Shows a real question's picture on the screen, with no microphone or speaker needed. |
| `data/pictures/` | Cached screen pictures, one per search (subject plus category). Safe to delete; it refills itself. |
| `data/chatbox.db` | SQLite: per-day question counts, the pause switch, plus the exchange log when logging is on. |

## Sessions, logging, and models

- **Sessions:** within one `chatbox chat` run, follow-up questions ("why?") see the earlier
  exchanges, up to the policy's `answers.remember_exchanges` ("Earlier questions it remembers" on the settings page). Nothing is remembered between runs.
- **Logging** is on by default (`[logging] enabled = true`). Every exchange is saved in
  `data/chatbox.db` on the machine running Chatbox: the time, the question (as typed, or as
  transcribed from speech), the answer, each pipeline step's decision, and the input and output
  tokens of every model call it made (classifier, answer, output check) with an estimated cost.
  That's what the settings page's "Today's questions and answers" and `chatbox log` show.
  The cost uses the per-model prices in `chatbox.toml` `[pricing]`; a model not listed there
  is logged as "cost unknown". An answer thrown away because the classifier said no is still
  billed, and counted if it had finished by then. Nothing is deleted
  automatically, and no audio is ever saved. Set it to `false` to keep only a count of
  questions per day (for the daily limit), with no question or answer text.
- **Model:** `[provider] name` picks the vendor (`openai` or `anthropic`) for the answer
  and both guardrail checks at once. Each vendor's models are set under
  `[provider.<vendor>]` and `[guardrails.classifier.<vendor>]` /
  `[guardrails.output_check.<vendor>]`. For OpenAI, keep `reasoning_effort = "none"`:
  without it the model reasons first, which is slower and costs more. For
  `claude-haiku-4-5`, leave out `effort`; Haiku doesn't accept it.

## The animal guessing game

Chatbox starts in question-answering mode. Say "I want to play the animal guessing game"
and it switches to a game: it thinks of an animal, and the children ask yes-or-no questions
("Is it a mammal?", "Does it fly?", "Does it live in the jungle?"). It answers "Yes!",
"No!", "Maybe", "Sometimes" or "I don't know" until someone guesses it, then cheers.
A round also ends when a child gives up and hears the answer. After a round, "play again"
picks a new animal; anything else is answered normally, back in question-answering mode.
"I'm done playing" or "let's go back to question answering mode" ends a game partway
through (Chatbox says what the animal was).

- **Noticing the request:** the guardrail classifier, which already runs on every question,
  reports a `mode_change` along with its decision. There's no keyword matching, so the
  request doesn't need exact words, and it costs no extra model call. Starting and stopping
  get fixed replies with no model call at all.
- **The secret animal** is picked at random in code (`animal_game.ANIMALS`) and written into
  the game's system prompt, so it can't change partway through. The log shows it in a
  `game` step on every game question, so a parent can see it.
- **The end of a round** is noticed in code: the game's prompt forbids naming the animal
  until it's guessed or the child gives up, so an answer that names it ends the round. The
  log shows a `game` step marked `over`.
- **The same guardrails** apply during the game: limits, classifier, canned replies, and the
  output check. A refused question gets the usual reply and the game carries on.
- **Memory:** during a game, the answering model sees only the game so far, up to
  `answers.remember_exchanges`. A game lasts only as long as the session.
- **Limits** (the settings page's **Games** tab, or `games.animal_guessing` in the policy):
  - `enabled` turns the game on or off. Off, a request to play gets `unavailable_reply`,
    and a game in progress ends at the next question.
  - `daily_games` caps the games *finished* per day: the animal was guessed, or the kid
    gave up and heard it. A game stopped partway through doesn't count. Once today's are
    used up, a request to play gets `unavailable_reply`.
  - Nothing in a game counts toward `limits.daily_questions`: asking to play, the game's
    questions, or stopping. The games have their own budget, so a game in progress
    carries on after the day's questions are used up, and a request to play can still
    start one. To tell that request apart, the classifier still runs on questions over
    the limit (a small cost), and anything else gets the daily-limit reply without an
    answer being asked for. With no games left, it's turned away with no model call, as
    before. The schedule and the pause apply to games as usual.

## The reading game

"Let's play the reading game" puts a word on the screen, and the kid reads it out loud.
Chatbox cheers a right answer, gives a small hint after a wrong one (its first sound, or
how many letters it has, never the word), and tells the word after three wrong tries or
when the kid says "I don't know" or "skip". A game is five words. After the last one it's
back to answering questions, unless someone says "play again". "I'm done playing" stops
partway through.

- **Reading level:** kindergarten to 5th grade, set on the settings page's **Games** tab
  (`games.reading.grade`: `K`, `1` … `5`). The words come from fixed lists in
  `reading_game.py`: mostly Dolch sight words for K–3, and common harder words
  ("adventure", "temperature") for 4th and 5th grade. Each game picks five at random,
  avoiding the last game's words.
- **Checking a word:** speech recognition often mishears a single word, and words like
  "the" and "it" turn up in any reply, so the answer can't be checked by looking for the
  word in it. Each try is one model call with structured output: the model reports
  whether it was the word (a homophone like "two" for "to" counts), a wrong try, giving up,
  or chat, plus a short reply. When to tell the kid the word is decided in code, in fixed
  words ("That word is jump. Good try!"), so it's never told too soon.
- **Limits** work like the animal guessing game's: on or off, games per day (a game counts
  once all five words are done), and a reply for when it can't play. Nothing said during
  the game counts toward the day's questions.
- **The screen** shows the word in large black letters on white for the whole game,
  including while the kid presses the button and reads. Without a screen (on a laptop),
  the word is printed in the terminal instead, so the game can be tried there.

## Parent settings page

```bash
chatbox talk --web      # or: chatbox chat --web
```

It prints the address to open, for example:

```
Parent settings: open http://192.168.1.20:8321/ on a phone or computer on this network.
```

Open that on any phone or computer on the home Wi-Fi. The page covers the whole policy,
in two tabs. **Questions** has name and tone, answer style, household age, the three topic
lists (add, edit, remove, or move a topic between lists by changing its "List"), each
ask-a-parent topic's reply, the schedule and timezone, the daily limit, and every fixed
reply. Honesty about being a computer is shown but can't be switched off. **Games** has the
[animal guessing game](#the-animal-guessing-game) and the [reading game](#the-reading-game):
for each, on or off, games per day, and what it says when it can't play, plus the reading
game's reading level (kindergarten to 5th grade). The tabs work without JavaScript and
are one form, so Save on either tab saves both, and the page comes back on the same tab. If
a save fails because of something on the other tab, that tab opens and is marked with a red
dot.

- **Right now** (top of the page) shows how many questions have been asked today against
  the daily limit, with two buttons:
  - **Reset today's count** starts today's count over at 0, so a kid who hit the limit
    can keep asking.
  - **Reset today's games** does the same for finished games. Each game's count is shown
    below the question count (or "Off" when that game is turned off).
  - **Pause Chatbox** makes every question get "I'm currently resting. Let's talk later."
    until you press **Resume**. It takes effect from the next question, beats the schedule
    and limit, and paused questions don't count. The pause is stored in `data/chatbox.db`,
    so restarting Chatbox doesn't undo it (it says PAUSED at startup).

  These buttons act at once and don't touch the policy file or any unsaved edits below.
  The count refreshes by itself every 5 seconds (the page's only bit of JavaScript), so
  unsaved edits are never lost to a reload.
- **Today's questions and answers** (collapsed, at the bottom) lists today's exchanges,
  newest first, with the time and whether it was a model answer or a fixed reply. It only
  has something to show when `[logging] enabled = true` in `chatbox.toml` (the default);
  otherwise it says logging is off. Reload the page to see new ones.
- **Save** checks the whole policy first. If anything is wrong (an empty reply, a window
  that ends before it starts, two topics with the same id), nothing is saved and each
  problem is shown in red next to its field. `policy_version` goes up by one on every save.
- **Preview prompt** shows the system prompt your unsaved changes would produce. The page
  always shows the version in use and, at the bottom, the system prompt in use now.
- If two people edit at once, the second save is refused with a message rather than
  silently undoing the first one's changes.

**How live updates work.** The settings page runs inside the Chatbox process, so it shares
Chatbox's in-memory policy. A save (1) checks the edit, (2) writes `policies/local.yaml`
by writing a temporary file and renaming it over the old one, so a crash can't leave a
half-written file, and (3) switches the in-memory policy. The next question uses the new
policy everywhere: system prompt, classifier and output check, schedule and daily limit,
and fixed replies. A question already being answered finishes under the policy it started
with. The file stays the durable copy: Chatbox reads it at startup. There is no separate
`chatbox serve`, because a second process would change the file without the running
Chatbox knowing. Without `--web`, edit the file and restart.

**Address.** `[web] host = "auto"` (the default) listens only on this computer's
home-network address, not on every network interface; `port` defaults to 8321. Set `host`
to a specific address, or `"127.0.0.1"` for this computer only. Requests from outside
private network ranges, and forms posted from other websites, are refused.

> **The page has no password.** Anyone on the home network can open it and change what
> Chatbox will talk about, including children and guests. A child on the same Wi-Fi could
> unblock topics or lift the daily limit. Only run it with `--web` while you're using it,
> and not on a network you share with people you don't trust.

**What happens to the file's comments.** A save rewrites `policies/local.yaml` from the
saved policy. The comment block at the top of the file is kept; any other comments, blank
lines, and YAML styles (such as `>-` for long text) are lost. The guidance that used to
live there is in this README and in the help text on the page.

## Editing the policy file

The policy is plain YAML, checked against a strict schema (`src/chatbox/policy.py`).
Unknown or misspelled keys are rejected, so typos don't silently do nothing. You can
still edit it by hand (with Chatbox stopped, or restart it afterwards):

1. Edit `policies/local.yaml` (this machine's copy; `[paths] policy` in `chatbox.toml`).
   To start over from the default, delete it: Chatbox copies `policies/default.yaml`
   again on its next run. Changes that should reach every machine go in
   `default.yaml`, but a machine that already has a `local.yaml` keeps its own.
2. Bump `policy_version` by one. Every log row records the version plus a content hash, so
   you can tell which rules produced which answer.
3. Run `chatbox check-policy`, then `chatbox show-prompt` to see what the model will be told.

What you can set:

- `household_age`: the age every answer is written for.
- `persona`: `name` and a list of `tone` words.
- `answers`: `max_sentences`, `max_words`, `say_when_unsure`, `ask_follow_up_question`, `remember_exchanges`.
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
                    │              → start / stop the guessing game ───────→ fixed reply
                    └ model answer ┘→ (allow) output check → pass: model answer
                                                          → fail: blocked-topic reply
```

The guardrail layers. The numbers are the design's
layer numbers, which the code comments use too; layer 2 (a provider's own content
filters) isn't used.

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
~1.3 s (in parallel), so ~1.4 s for an answered question and ~1.1 s for a redirect or
refusal. Turning the output check on adds ~1.2 s to an answered question.

**Fail closed.** If the classifier or the output check errors, times out, or returns
something unusable (wrong format, a redirect naming a blocked topic, and so on), the
kid hears `canned_replies.something_went_wrong`, never an unchecked answer.

**Explainable.** Every step records its decision, a short reason, and its time in
milliseconds. `chatbox chat -v` prints them; with logging on they're stored in
`steps_json`. A redirect looks like:

```
   · classify: redirect [1055 ms] death_and_loss: The child is asking about a pet dying...
   · canned: use [0 ms] death_and_loss
   · generate: discarded classifier said redirect
```

### Guardrail settings (`chatbox.toml`)

| Setting | Meaning |
|---|---|
| `[provider.<vendor>] timeout_seconds` | Deadline for the answering model. |
| `[guardrails] history_exchanges` | Recent question/answer pairs the checks see. |
| `[guardrails] length_tolerance` | Fraction over the policy's length limits still accepted (0.25 = 25%). |
| `[guardrails.output_check] enabled` | Off (`false`) by default: in testing it sometimes rejected harmless answers, such as how chefs chop vegetables. `false` turns layer 4 off entirely, length check included. Saves about 1 s per answered question, but answers are no longer checked before the kid hears them. `chatbox chat` shows "output check OFF" and each answer's steps show `output_check: skipped`. |
| `[guardrails.classifier]` / `[guardrails.output_check]` | Each has `timeout_seconds` (hard deadline; past it the pipeline fails closed) and `max_retries`, plus a `.anthropic` and an `.openai` subsection with that vendor's `model` and `max_tokens`. They use the same vendor as the answering model (`[provider] name`). |

## Voice

`chatbox talk` turns speech into text, runs the same text pipeline, and speaks the result:

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
- **Always audio feedback** (young children may not read yet): a rising beep when listening starts, a
  falling beep on release, a soft blip for a tap that was too short (nothing is sent), the
  policy's `didnt_catch_that` reply for silence or no recognized words, the policy's
  `something_went_wrong` reply when speech-to-text fails, and a low two-note error sound if
  text-to-speech itself fails.
- **Push-to-talk in a terminal.** Terminals report key presses but not releases. Holding
  a key makes macOS repeat it, so Chatbox treats "repeats stopped" as the release. If
  releases feel late or taps count as holds, tune `key_repeat_wait_seconds` and
  `release_gap_seconds`, or the keyboard repeat settings in System Settings → Keyboard.
  If key repeat is turned off entirely, every press looks like a tap.
- **Adapters.** The voice turn (`src/chatbox/voice.py`) never touches devices. The laptop
  adapter (`src/chatbox/audio/laptop.py`) supplies the mic, speaker and spacebar. The Pi's
  (`src/chatbox/audio/pi.py`) adds the arcade button, the status LED and the screen, each
  behind a wrapper so the voice turn stays free of hardware.
- **Swappable services.** `SpeechToText` and `TextToSpeech` in `src/chatbox/speech/base.py`.
  To add a vendor, add a class and a name in `make_stt` / `make_tts`.

### Speech settings (`chatbox.toml`)

| Setting | Meaning |
|---|---|
| `[speech] language` | Language for both services (`en-US`). |
| `[speech.stt] name` | Speech-to-text vendor (`google`). |
| `[speech.tts] name` | Text-to-speech vendor: `google` (default) or `openai`. Both vendors' sections stay filled in, so switching is one line. |
| `[speech.stt.google] model`, `location` | Recognition model and its region. Measured time from release to transcript: `long` / `global` ≈ 0.13 s; `chirp_3` / `us` ≈ 0.75 s; `chirp_2` / `us-central1` ≈ 0.8 s. The Chirp models are newer and may cope better with young children's speech; worth comparing on your own children's voices. |
| `[speech.tts.google] voice` | Any Google voice name, e.g. `en-US-Chirp3-HD-Leda` or `en-US-Neural2-J`. Chirp 3 HD voices stream, so audio starts sooner; other voices (Neural2, WaveNet, …) are made whole and then played. |
| `[speech.tts.openai] voice`, `model`, `instructions` | An OpenAI voice (`marin`), the model (`gpt-4o-mini-tts`), and a plain-words description of how to speak. Needs `OPENAI_API_KEY` in `.env`. OpenAI requires telling listeners the voice is AI-generated. |
| `[voice] min_press_seconds` | Shorter presses count as accidental taps. |
| `[voice] silence_rms` | Recordings quieter than this get "didn't catch that" without calling speech-to-text. |
| `[voice] stt_timeout_seconds` | Deadline for the transcript after release; past it, the kid hears `something_went_wrong`. |
| `[voice] max_seconds` | Longest recording. |
| `[voice] key_repeat_wait_seconds`, `release_gap_seconds` | Spacebar hold/release detection (see above). |
| `[voice] input_device`, `output_device`, `sample_rate` | Audio devices and mic sample rate. |

`chatbox talk -v` shows the steps: `stt` (time from release to transcript), each text
pipeline step, `pipeline` (total), `tts` (time to first audio), and `speech_start` (end of
question to first audio, which should stay under 5,000 ms).

## The screen (Pi only)

A 2.2" SPI display showing a picture of what was asked about while Chatbox answers.
**Pictures only, never text** — young children may not read yet, so the
screen supplements the spoken answer and is never needed to understand it. The exception
is [the reading game](#the-reading-game): its word stays on the screen for the whole game,
between turns too, and no pictures are looked up meanwhile.

```
classifier (already running) ─▶ subject + category ─▶ Wikipedia article's lead image ─▶ cache
                                                                                        │
        answer starts playing ─▶ show whatever is ready ─── turn ends ─▶ blank
```

- **The subject comes from the guardrail classifier**, which already runs on every
  question, so there is no extra model call and no extra latency. `Classification.subject`
  is the concrete thing the question is about (`"octopus"`, `"the Moon"`), or `None`
  where there isn't one ("why do we have to sleep?"). It never affects a decision.
- **Only kinds of thing a parent listed get a picture.** The policy's
  `pictures.categories` (edited on the settings page) lists them: `animal`, `space`,
  `vehicle` and so on. The classifier picks one for the subject, or none, and the
  category is added to the search, so "python" is looked up as `python animal` and finds
  the snake rather than the programming language. No category means no picture. The
  bare subject is searched first and kept when an article is titled with it exactly
  ("Eiffel Tower"), because adding the category to an unambiguous subject can pull the
  search off course ("Eiffel Tower landmark" finds a replica in Texas).
- **An animal battle shows both animals.** For "who would win, a shark or a crocodile?"
  the classifier also reports an `opponent`, kept only when the category is `animal`.
  Both pictures are looked up, one after the other, and while the answer is spoken the
  screen swaps between them every `[screen] swap_seconds` (3 by default). If only one is
  found, that one stays up.
- **Landscape, 320x240.** The panel is 240x320 glass turned 90 degrees (`[screen]
  rotation`), because a photograph of a thing is usually wider than it is tall.
  Pictures are letterboxed to that shape on black, never stretched.
- **The picture is the Wikipedia article's lead image** for that subject — not a
  Wikimedia Commons free-text image search, which ranks every file anyone has ever
  uploaded against a word. One API request per search, then a disk cache, including a
  negative entry so a subject with no picture is looked up once.
- **Speech never waits for a picture.** The lookup runs on its own thread with a hard
  timeout (`[screen] timeout_seconds`). Whatever is ready when the answer starts is
  shown; anything still in flight appears late, or not at all. The 5 s target from question
  to speech is untouched.
- **Nothing can break a turn.** No article, no network, a dead panel, a missing library:
  every one of them means no picture and an identical spoken answer.
- **Blocked and redirected questions show nothing.** Only an "allow" starts a lookup.
- **Between turns the screen is blank**, and with `backlight_gpio` set the backlight
  goes out with it, so it's genuinely dark rather than a lit grey rectangle.
- **`chatbox chat` uses it too**, not just `chatbox talk`: the picture goes up with the
  printed answer and comes down when the next question is typed. That is the only way
  to see a whole turn on a Pi without a microphone and speaker. It appears
  a moment *after* the text, because the lookup only starts when the classifier returns
  and there is no speech to cover it.
- **Adapters, not plumbing.** `chatbox/voice.py` and the pipeline know nothing about the
  screen, exactly as with the status light. `chatbox/audio/picture.py` wraps the
  classifier (to catch the subject) and the speaker (to show and clear); the panel itself
  is `Screen` in `chatbox/audio/pi.py`, lazily imported.

> **The picture is not checked by any guardrail before a child sees it.** The classifier
> checks the *question*; nothing checks the image that comes back. This is a real gap in
> the promise that nothing reaches a child unchecked. Only turn the screen on if an adult
> is nearby while Chatbox is used. The picture categories narrow what can be looked up,
> but they don't check the image either.

### Screen settings (`chatbox.toml`)

The screen is off unless a machine turns it on, so these live in `chatbox.local.toml`
on the Pi; `chatbox.toml` documents them and ships with `enabled = false`.

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
pytest                 # also runs the live smoke tests when the chosen vendor's key is set
CHATBOX_PROVIDER=anthropic pytest -m live -s   # the same live tests on the other vendor
pytest -m live -s      # just the live smoke tests, printing each step and its time
                       # (the voice one needs the Google keys; it plays tests/fixtures/question.wav,
                       #  a synthetic voice, through real speech-to-text and text-to-speech.
                       #  The screen's live test calls the real Wikipedia API and needs no key.)
```

Unit tests use a frozen copy of the policy (`tests/fixtures/policy.yaml`), so editing
`policies/local.yaml` or `policies/default.yaml` doesn't break them. The live tests use it too.
