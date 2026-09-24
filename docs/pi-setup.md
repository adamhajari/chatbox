# Running Chatbox on a Raspberry Pi

Everything here is for a **Raspberry Pi 3 Model B v1.2** with **no audio hardware**: the
text pipeline (`chatbox chat`) and the parent settings page. The microphone, speaker,
amplifier, button and LEDs come later; `chatbox talk` is expected to refuse to start
until they're plugged in.

Its own doc rather than a README section: the README describes Chatbox itself, and none
of this applies when you run on the laptop. Skip to [Measuring the Pi](#8-measuring-the-pi)
once it's running.

Each step says how to check it worked and what the failure looks like. Steps 1–3 are on
the Mac; 4 onward are on the Pi over SSH.

---

## 1. Write the SD card (on the Mac)

Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Use a card of 8 GB
or more; writing it erases everything on it.

- **Raspberry Pi Device:** Raspberry Pi 3
- **Operating System:** *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**
- **Storage:** your SD card

**Lite, and 64-bit, both matter.**

*Lite* has no desktop. Chatbox is reached over SSH and from a phone browser, so a desktop
only costs memory, and the Pi 3B has 1 GB.

*64-bit* is what makes the install bearable. Chatbox's Google speech libraries pull in
`grpcio`, which is a large C++ extension. On 64-bit (`aarch64`) pip downloads a prebuilt
wheel and it's done in a minute; on 32-bit (`armv7l`) there is no wheel, so pip compiles
gRPC from source, which on a Pi 3B takes hours and often dies when it runs out of memory.
The same is true of `pydantic-core`. A Pi 3B is 64-bit capable, so take the wheels.

Before writing, open **⚙ / Edit Settings** and fill in:

| Setting | Value |
|---|---|
| Hostname | `chatbox` (this doc assumes it; the Pi is then `chatbox.local`) |
| Username and password | pick your own; this doc writes it as `<user>` |
| Wi-Fi SSID / password | your home network — the Pi 3B is **2.4 GHz only**, so use your 2.4 GHz network name if your router splits the bands |
| Wireless LAN country | your country, or Wi-Fi stays off |
| Locale / time zone | your time zone. It matters: the policy's schedule and the daily question cap are evaluated in the policy's own time zone, but logs and `date` follow the Pi's |
| **Services → Enable SSH** | **on**, "Use password authentication" (or paste a public key if you have one) |

Write the card, wait for the verify pass, eject it, put it in the Pi, and power it on.

**Verify:** the green activity LED flickers for a minute or two on first boot. Solid green
with no flicker after a few minutes usually means the card didn't write properly — rewrite it.

---

## 2. Connect from the Mac

```sh
ssh <user>@chatbox.local
```

Accept the host key fingerprint the first time.

**Verify:** you get a `<user>@chatbox:~ $` prompt.

**When `chatbox.local` doesn't resolve** (`ssh: Could not resolve hostname chatbox.local`),
mDNS isn't reaching it. In order:

1. Give it another minute; first boot expands the filesystem and reboots once.
2. Find it by address instead. Either check your router's list of attached devices, or
   from the Mac:
   ```sh
   dns-sd -B _ssh._tcp            # Ctrl-C to stop; look for "chatbox"
   arp -a | grep -i -e b8:27:eb -e dc:a6:32 -e e4:5f:01   # Raspberry Pi MAC prefixes
   ```
   Then `ssh <user>@192.168.x.y`.
3. If nothing appears at all, the Pi isn't on the network: the most common causes are a
   5 GHz-only SSID, a typo'd Wi-Fi password, or a missing "Wireless LAN country". All
   three mean rewriting the card, since the Pi never got far enough to log anything you
   can read. A wired Ethernet cable is the quickest way to rule Wi-Fi out.

**If the host key changed** (after reflashing the same hostname), the Mac refuses to
connect with a large warning. Clear the old key:
```sh
ssh-keygen -R chatbox.local
```

---

## 3. Note the Pi's address

On the Pi:
```sh
hostname -I | awk '{print $1}'
```
Write it down; you'll need it for the settings page on a phone, and to get back in if
mDNS is flaky.

Home routers hand out addresses on a lease, so this can change after a reboot. Two ways
to make it dependable, either is fine:

- **Preferred:** reserve it in the router. Find "DHCP reservation" / "static lease" and
  pin the Pi's MAC address (`ip link show wlan0`, the `link/ether` value) to one address.
  The Pi needs no changes.
- Or just re-run `hostname -I` over `ssh <user>@chatbox.local` each time.

---

## 4. First-boot housekeeping

```sh
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

Expect this to take 10–20 minutes on a Pi 3B the first time. Reconnect after the reboot.

Check what Python the image ships:
```sh
python3 --version
```

Chatbox needs **3.11 or newer** (`requires-python = ">=3.11"` in `pyproject.toml`).
Raspberry Pi OS Lite based on Debian 12 (Bookworm) ships 3.11; the Debian 13 (Trixie)
image ships 3.13. Both are fine — **don't install a newer Python yourself**, and don't
use `pyenv` here. Building Python from source on a Pi 3B is slow, and the system one is
what the prebuilt wheels are published for.

If `python3 --version` reports 3.9 or older, you're on an old image. Reflash from step 1
rather than fighting it.

Install the two system packages the image doesn't have:
```sh
sudo apt install -y git python3-venv
```

**Verify:** `git --version` and `python3 -m venv --help` both print something.

---

## 5. Get Chatbox onto the Pi

First, **commit anything you want on the Pi**, on the Mac. A clone copies committed
history only, so uncommitted work stays behind.

Chatbox has no GitHub remote, and doesn't need one: clone straight from the Mac. Turn on
System Settings → General → Sharing → **Remote Login** there, then on the Pi:

```sh
cd ~
git clone <mac-user>@<mac-hostname>.local:/path/to/chatbox chatbox
cd chatbox
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

That leaves a normal git remote pointing at the Mac, so later updates are `git pull`
with the Mac awake and on the same Wi-Fi. If the Mac's `.local` name doesn't resolve from
the Pi, use its address (`ipconfig getifaddr en0` on the Mac). `.env`, `.venv/` and
`data/` are gitignored, so none of them come across — step 6 sets them up on the Pi.

If you'd rather not leave Remote Login on, a private GitHub repo works the same way and
survives the Mac being asleep. Keep it **private**: the policy file and `PLAN.md` describe
your household.

The venv isn't optional. Raspberry Pi OS marks its system Python as externally managed,
so `pip install` outside a venv fails with `error: externally-managed-environment`. That
message is the system protecting itself — make the venv, don't pass `--break-system-packages`.

**What to expect from the install.** On a Pi 3B over Wi-Fi this takes roughly 5–10
minutes, nearly all of it downloading. Watch the lines as they scroll:

- `Downloading grpcio-…-manylinux_2_17_aarch64.whl (6.0 MB)` — good, that's the prebuilt
  wheel, and the same for `pydantic_core-…aarch64.whl`.
- `Building wheel for grpcio (setup.py)` — **stop it with Ctrl-C.** That means pip found
  no wheel for this machine and is about to compile gRPC, which on a Pi 3B takes hours and
  usually ends in `g++: fatal error: Killed signal terminated program cc1plus`, the
  out-of-memory killer. The cause is almost always a 32-bit image. Check with
  `uname -m`: it must print `aarch64`. If it prints `armv7l`, reflash with the 64-bit
  image (step 1); that is faster than compiling.

**Verify:**
```sh
.venv/bin/python -c "import anthropic, grpc, pydantic; print('ok')"
```

---

## 6. Secrets

Same rules as the laptop: **nothing secret is ever committed**, and the Google key file
lives outside the repo with tight permissions. `.env` is already in `.gitignore`.

```sh
cd ~/chatbox
cp .env.example .env
nano .env          # Ctrl-O to save, Ctrl-X to quit
chmod 600 .env
```

Set `ANTHROPIC_API_KEY` to your key. That's all `chatbox chat` needs.

The Google credentials are only used by `chatbox talk`, so you can skip them until the
microphone arrives. When you do want them, copy the service-account key from the Mac —
over `scp`, never through the repo:

```sh
# on the Pi
mkdir -p ~/.config/chatbox && chmod 700 ~/.config/chatbox

# on the Mac
scp ~/.config/chatbox/gcp-speech.json <user>@chatbox.local:~/.config/chatbox/

# back on the Pi
chmod 600 ~/.config/chatbox/gcp-speech.json
```

and point `.env` at it:
```
GOOGLE_APPLICATION_CREDENTIALS=/home/<user>/.config/chatbox/gcp-speech.json
GOOGLE_CLOUD_PROJECT=your-project-id
```
Use the full path, not `~` — it's read as a literal path, and `~` silently fails to resolve.

**Verify:**
```sh
ls -l .env ~/.config/chatbox/          # both should show -rw------- (600)
git status --short                     # must NOT list .env or any key file
```

---

## 7. Run it

All commands run from `~/chatbox` with the venv's Python. `.venv/bin/chatbox` is the
installed command; `source .venv/bin/activate` first if you'd rather type `chatbox`.

```sh
cd ~/chatbox
.venv/bin/chatbox check-policy      # OK: …/policies/default.yaml (version …)
.venv/bin/chatbox show-prompt       # prints the compiled system prompt
.venv/bin/chatbox chat -v           # ask a question; -v prints the pipeline steps
```

**Common failures**

| What you see | What it means |
|---|---|
| `Config file not found: chatbox.toml` | you're not in `~/chatbox`; `cd` there first |
| `Policy file not found: …` | same |
| `Could not resolve authentication method` / 401 from the API | `ANTHROPIC_API_KEY` is missing or wrong in `.env` |
| answers are all the "something went wrong" reply | the Pi can't reach the API. Check `ping -c3 api.anthropic.com`, and that the clock is right (`date`) — a wrong clock breaks TLS |

**`chatbox talk` without a microphone** is expected to refuse, and it should say so in a
sentence:

```
$ .venv/bin/chatbox talk
chatbox talk needs a microphone and a speaker, but the PortAudio sound library isn't
installed (…). On Raspberry Pi OS / Debian: sudo apt install -y libportaudio2
`chatbox chat` works without them.
```

Installing `libportaudio2` on a Pi with still no devices attached changes the message to
"no microphone or speaker was found", which is also correct. Either way it's one line,
not a stack trace. If you ever get a stack trace out of `chatbox talk`, that's a bug —
the check lives in `audio_device_problem()` in `src/chatbox/audio/__init__.py`.

---

## 8. The parent settings page from a phone

```sh
cd ~/chatbox
.venv/bin/chatbox chat --web
```

It prints the address to open:
```
Parent settings: open http://192.168.x.y:8321/ on a phone or computer on this network.
```

Open that on a phone on the same Wi-Fi. Change a topic, save, then ask the next question
in the same terminal — it follows the new policy without a restart (PLAN.md D20).

**What the Pi needs that the Mac didn't:** nothing in the code, and no firewall change
(Raspberry Pi OS ships with no firewall enabled). The `[web] host = "auto"` setting finds
the address the Pi's default route uses, which is the Wi-Fi address — it does **not** rely
on the hostname, so Debian's `127.0.1.1` hostname entry can't trap it.

Two things to know:

- **The page has no password** (PLAN.md D7a). Anyone on the home network can change the
  policy. Requests from outside private address ranges are refused, but that's all.
- **Nothing starts at boot.** Running Chatbox as a service is deliberately out of scope
  for now: you SSH in and start it by hand. Closing the SSH session kills it. If you want
  it to survive the session, start it under `tmux`:
  ```sh
  sudo apt install -y tmux
  tmux new -s chatbox           # run chatbox in here; Ctrl-B then D to detach
  tmux attach -t chatbox        # come back to it
  ```

**If the phone can't reach it:** check the phone is on the same Wi-Fi (not cellular, and
not a guest network — guest networks block device-to-device traffic, which is the usual
cause). `curl -sI http://127.0.0.1:8321/` on the Pi itself tells you whether the server
is up or the network is in the way.

---

## 9. Measuring the Pi

This is what decides whether a Pi 5 is worth buying. `scripts/measure.py` runs the real
CLI and reports the same per-step timings `chatbox chat -v` prints:

```sh
cd ~/chatbox
.venv/bin/python scripts/measure.py -n 3
```

It asks four fixed questions three times, then prints medians for start-up, each pipeline
step, end-to-end time, and the peak memory one Chatbox process used. Run the same command
on the Mac for the comparison. Costs a few cents of API usage per run.

Check memory while it's running, from a second SSH session:
```sh
free -m           # "available" is the number that matters, against ~1 GB total
```

### Mac baseline (2026-09-22, MacBook, Python 3.13.9, Haiku 4.5, output check off)

| | median | range |
|---|---|---|
| start → first `kid>` prompt | 0.42 s | 0.41–0.42 |
| question, end to end | 1.42 s | 1.09–5.18 |
| · `classify` | 1.22 s | 1.04–2.84 |
| · `generate` | 1.34 s | 0.95–5.18 |
| peak memory, one process | 76 MB | |

`classify` and `generate` run concurrently, so the end-to-end time is roughly the slower
of the two, not their sum. `output_check` reads 0 ms because it's off (D21); turning it
on adds about a second. The wide upper range is API tail latency, not the machine.

### Pi 3B (measured 2026-09-22, Python 3.13.5, same model and settings)

| | median | range |
|---|---|---|
| start → first `kid>` prompt | 7.59 s | 7.58–7.70 |
| question, end to end | 1.40 s | 1.12–4.12 |
| · `classify` | 1.34 s | 1.05–4.11 |
| · `generate` | 1.33 s | 1.05–1.79 |
| peak memory, one process | 70 MB | of 1 GB |

Per question the Pi 3B is **level with the Mac** (1.40 s against 1.42 s). Start-up is 18×
slower — imports read off an SD card — and is paid once per run, not per question.

An earlier run of the same command on the same board gave a median of 2.77 s with an
11.6 s worst case. Nothing was changed between the two. `scripts/probe_net.py` explains
why that run was not the board's fault:

| | Mac | Pi 3B |
|---|---|---|
| tcp connect | 9 ms | 8 ms |
| tls handshake | 17 ms | 41 ms |
| warm API call | 506 ms | 547 ms |
| two concurrent calls | 528 / 609 ms | 537 / 764 ms |

The link, the TLS handshake and concurrent calls are all at parity, so the spikes were
API-side or network-side variance. The Mac shows the same tail in miniature: a 5.5 s
worst case on a full-length answer, on a fast machine and a 5 GHz link.

### Verdict: stay on the Pi 3B

**Don't buy a Pi 5.** Per-question time matches the laptop, memory sits at 70 MB of 1 GB,
and nothing measured here is bounded by the board. Revisit only if the voice path (phase
6b) pushes memory near the limit once gRPC and audio buffers are loaded, or if a local
wake word is ever added (PLAN.md D5) — that is real local compute, and the only workload
in this project that would be.

The one thing a Pi 5 would buy today is a 5 GHz radio; the Pi 3B is 2.4 GHz only. That
did not show up as a problem in these measurements, but it is the thing to suspect if
latency gets worse once the box lives in a kid's room, further from the router.

### Open: the classifier deadline is too tight

`[guardrails.classifier] timeout_seconds = 4` is a hard deadline. Past it the pipeline
fails closed: the answer that was generated concurrently is discarded and the child hears
the "something went wrong" reply.

At 4 s this failed **7 of 12 questions** on the Pi. Even in the good run above, with the
deadline raised for measurement, one classify took 4.11 s — it would have failed closed.
The Mac is not safe either: its worst classify was 2.84 s against the same 4 s ceiling.

Raising it is not free: D4 budgets 5 s from the end of a question to the start of speech,
and a long deadline means a child stands there waiting. This needs a decision, not a
tuning tweak. The measured numbers to decide from: classify is ~1.2–1.3 s typically, and
its tail reached 4.1 s in a good run and 11.5 s in a bad one.

### How to read it

Chatbox does almost no local work: classification, the answer and (later) speech are all
network calls. The Pi's CPU only has to start Python, parse the policy and do TLS. So
expect:

- **Start-up** to be several times slower than the Mac — importing `anthropic`, `pydantic`
  and `grpc` off an SD card is disk-bound and single-thread-bound. It happens once per run.
- **Per question** to be close to the Mac, because it's dominated by the same API round
  trips. A Pi 3B that lands within a few hundred milliseconds of the Mac per question is
  doing its job.
- **Memory** to be the real risk, not speed: 1 GB total, with `grpc` loaded once the
  voice path is added.

**The decision rule:** the budget is 5 s from the end of a question to the start of speech
(PLAN.md D4), and voice adds speech-to-text and text-to-speech on top of what's measured
here. Buy a Pi 5 if, on the Pi 3B, **per-question time is more than about a second worse
than the Mac**, or **memory available under load drops below ~150 MB**. Slow start-up on
its own is not a reason — it's paid once, and once Chatbox runs as a service it's paid at
boot.
