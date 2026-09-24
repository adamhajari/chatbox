# Developing Chatbox on a Mac, running it on a Pi

For working on Chatbox itself: you edit and test on a Mac, and the Raspberry Pi is where
it actually runs. To set up a Pi from scratch, follow [pi-setup.md](pi-setup.md) first.
This doc starts from a Pi that already has a clone and a working `.venv`.

The code moves in one direction only: **Mac → Pi.** Don't edit on the Pi. A fix made
there has to be copied back by hand, and it's easy to lose the next time you pull.

---

## Getting code onto the Pi

There are two routes, and a Pi can have both set up.

### Through GitHub (the normal route)

On the Mac, commit and push:

```sh
git push
```

On the Pi:

```sh
cd ~/chatbox
git pull
```

The install is editable (`pip install -e .`), so pulled code runs as soon as it lands.
Reinstall only when `pyproject.toml` changed, i.e. when a dependency was added or bumped:

```sh
.venv/bin/pip install -e .
```

### Straight from the Mac (for work you haven't pushed)

This is for trying a branch on real hardware before it goes to GitHub. The Pi fetches
from the Mac's working copy over SSH, so nothing has to be pushed first.

1. On the Mac, turn on System Settings → General → Sharing → **Remote Login**.
2. On the Pi, add the Mac as a second remote, once:

   ```sh
   cd ~/chatbox
   git remote add mac <mac-user>@<mac-hostname>.local:/path/to/chatbox
   ```

   If the Mac's `.local` name doesn't resolve from the Pi, use its address instead
   (`ipconfig getifaddr en0` on the Mac).

3. **Commit on the Mac.** A fetch copies commits only, so uncommitted changes stay
   behind. A throwaway `wip` commit is fine; you can amend or squash it before pushing.
4. On the Pi, fetch and switch to the branch:

   ```sh
   git fetch mac
   git switch -C my-branch mac/my-branch     # -C resets it if it already exists here
   ```

   To go back afterwards: `git switch main && git pull`.

The Mac has to be awake and on the same network for this. That's why GitHub is the
normal route.

---

## What doesn't travel

These are gitignored, so neither route copies them. Each machine keeps its own:

| File | What it is | On the Pi |
|---|---|---|
| `.env` | API keys | set up once in pi-setup.md step 6 |
| `chatbox.local.toml` | per-machine settings: GPIO pins, audio devices, the screen | set up once; see `chatbox.local.toml.example` |
| `data/` | the session database and logs | created on first run |
| `~/.config/chatbox/gcp-speech.json` | the Google service-account key | copied over with `scp`, never through the repo |
| `PLAN.md`, `prompts/` | planning notes and session prompts | not needed on the Pi |

To copy the Google key to the Pi:

```sh
# on the Pi
mkdir -p ~/.config/chatbox && chmod 700 ~/.config/chatbox

# on the Mac
scp ~/.config/chatbox/gcp-speech.json <user>@chatbox.local:~/.config/chatbox/

# back on the Pi
chmod 600 ~/.config/chatbox/gcp-speech.json
```

A setting that should be the same everywhere belongs in `chatbox.toml`, which is tracked.
A setting that differs between the Mac and the Pi belongs in `chatbox.local.toml`. If
you find yourself editing `chatbox.toml` on the Pi, it's the second kind.

---

## Measuring the Pi against the Mac

`scripts/measure.py` runs the real CLI and reports the same per-step timings
`chatbox chat -v` prints. Run the same command on both machines to compare:

```sh
cd ~/chatbox
.venv/bin/python scripts/measure.py -n 3
```

It asks four fixed questions three times, then prints medians for start-up, each pipeline
step, end-to-end time, and the peak memory one Chatbox process used. Costs a few cents of
API usage per run.

Check memory while it's running, from a second SSH session:

```sh
free -m           # "available" is the number that matters, against ~1 GB total
```

`scripts/probe_net.py` separates the network from the machine: TCP connect, TLS
handshake, and one warm and two concurrent API calls.

### Mac baseline (2026-09-22, MacBook, Python 3.13.9, Haiku 4.5, output check off)

| | median | range |
|---|---|---|
| start → first `kid>` prompt | 0.42 s | 0.41–0.42 |
| question, end to end | 1.42 s | 1.09–5.18 |
| · `classify` | 1.22 s | 1.04–2.84 |
| · `generate` | 1.34 s | 0.95–5.18 |
| peak memory, one process | 76 MB | |

`classify` and `generate` run concurrently, so the end-to-end time is roughly the slower
of the two, not their sum. `output_check` reads 0 ms because it's off; turning it on adds
about a second. The wide upper range is API tail latency, not the machine.

### Pi 3B (2026-09-22, Python 3.13.5, same model and settings)

| | median | range |
|---|---|---|
| start → first `kid>` prompt | 7.59 s | 7.58–7.70 |
| question, end to end | 1.40 s | 1.12–4.12 |
| · `classify` | 1.34 s | 1.05–4.11 |
| · `generate` | 1.33 s | 1.05–1.79 |
| peak memory, one process | 70 MB | of 1 GB |

Per question the Pi 3B is **level with the Mac** (1.40 s against 1.42 s). Start-up is 18×
slower, because imports are read off an SD card, and is paid once per run, not per
question.

An earlier run of the same command on the same board gave a median of 2.77 s with an
11.6 s worst case. Nothing was changed between the two. `scripts/probe_net.py` shows that
run was not the board's fault:

| | Mac | Pi 3B |
|---|---|---|
| tcp connect | 9 ms | 8 ms |
| tls handshake | 17 ms | 41 ms |
| warm API call | 506 ms | 547 ms |
| two concurrent calls | 528 / 609 ms | 537 / 764 ms |

The link, the TLS handshake and concurrent calls are all at parity, so the spikes were
API-side or network-side variance. The Mac shows the same tail in miniature: a 5.5 s
worst case on a full-length answer, on a fast machine and a 5 GHz link.

### Verdict: the Pi 3B is enough

Per-question time matches the laptop, memory sits at 70 MB of 1 GB, and nothing measured
is bounded by the board. Revisit if the voice path pushes memory near the limit once
gRPC and audio buffers are loaded, or if a local wake word is ever added. That would be
real local compute, the only workload in this project that is.

The one thing a Pi 5 would buy is a 5 GHz radio; the Pi 3B is 2.4 GHz only. That didn't
show up as a problem in these measurements, but it's the thing to suspect if latency gets
worse once the box sits further from the router.

**The rule for re-measuring:** the budget is 5 s from the end of a question to the start
of speech, and voice adds speech-to-text and text-to-speech on top of what's measured
here. A faster board is worth it if, on the Pi 3B, **per-question time is more than about
a second worse than the Mac**, or **memory available under load drops below ~150 MB**.
Slow start-up on its own is not a reason: it's paid once per run.

### The classifier deadline: 9 s

`[guardrails.classifier] timeout_seconds` is a hard deadline. Past it the pipeline fails
closed: the answer that was generated concurrently is discarded and the child hears the
"something went wrong" reply.

It was 4 s, and at 4 s it failed **7 of 12 questions** on the Pi. Even in the good run
above, with the deadline raised for measurement, one classify took 4.11 s and would have
failed closed. The Mac wasn't safe either: its worst classify was 2.84 s. So the default
is now 9 s, on every machine.

The cost: the 5 s budget means a slow classify leaves a child waiting, and 9 s allows a
wait well past it. That only happens in the tail. Classify is ~1.2–1.3 s typically, so a
typical question is unaffected, and a slow answer beats a "something went wrong" for a
question that would have passed. Its tail reached 4.1 s in a good run and 11.5 s in a bad
one, so even 9 s fails closed in a bad run.
