# Setting up Chatbox on a Raspberry Pi

This takes you from a blank SD card to Chatbox answering questions on a Raspberry Pi,
with its settings page open on your phone. It was written for, and tested on, a
**Raspberry Pi 3 Model B v1.2**.

Every step says how to check it worked and what the usual failure looks like. Steps 1–2
happen on your computer; everything after that happens on the Pi, over SSH.

## What you need

- A Raspberry Pi 3 Model B, a 5 V 2.5 A micro-USB power supply, and a microSD card of
  8 GB or more
- A computer with an SD card reader, on the same Wi-Fi network the Pi will join
- An [Anthropic API key](https://console.anthropic.com/)
- For voice (`chatbox talk`): a Google Cloud service-account key, set up as described in
  the README's [Voice setup](../README.md#voice-setup-for-chatbox-talk)
- For voice: the button, light, amplifier, speaker and microphone from
  [hardware.md](hardware.md). You can do steps 1–7 without them, since text chat needs
  none of it.

---

## 1. Write the SD card (on your computer)

Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Writing the card
erases everything on it.

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
The same is true of `pydantic-core`.

Before writing, open **⚙ / Edit Settings** and fill in:

| Setting | Value |
|---|---|
| Hostname | `chatbox` (this doc assumes it; the Pi is then `chatbox.local`) |
| Username and password | pick your own; this doc writes the username as `<user>` |
| Wi-Fi SSID / password | your home network. The Pi 3B is **2.4 GHz only**, so use your 2.4 GHz network name if your router splits the bands |
| Wireless LAN country | your country, or Wi-Fi stays off |
| Locale / time zone | your time zone. It matters: the policy's schedule and daily question limit use the policy's own time zone, but logs and `date` follow the Pi's |
| **Services → Enable SSH** | **on**, "Use password authentication" (or paste a public key if you have one) |

Write the card, wait for the verify pass, eject it, put it in the Pi, and power it on.

**Verify:** the green activity LED flickers for a minute or two on first boot. Solid green
with no flicker after a few minutes usually means the card didn't write properly, so
rewrite it.

---

## 2. Connect to the Pi (from your computer)

```sh
ssh <user>@chatbox.local
```

Accept the host key fingerprint the first time.

**Verify:** you get a `<user>@chatbox:~ $` prompt.

**When `chatbox.local` doesn't resolve** (`ssh: Could not resolve hostname chatbox.local`),
your computer can't find the Pi by name. In order:

1. Give it another minute. First boot expands the filesystem and reboots once.
2. Find it by address instead. Check your router's list of attached devices, or from
   your computer:
   ```sh
   arp -a | grep -i -e b8:27:eb -e dc:a6:32 -e e4:5f:01   # Raspberry Pi hardware prefixes
   dns-sd -B _ssh._tcp                                     # Mac only; Ctrl-C to stop
   ```
   Then `ssh <user>@192.168.x.y`.
3. If nothing appears at all, the Pi isn't on the network. The most common causes are a
   5 GHz-only network, a mistyped Wi-Fi password, or a missing "Wireless LAN country".
   All three mean rewriting the card. A wired Ethernet cable is the quickest way to rule
   Wi-Fi out.

**If the host key changed** (after rewriting the card with the same hostname), SSH
refuses to connect with a large warning. Clear the old key:
```sh
ssh-keygen -R chatbox.local
```

---

## 3. Note the Pi's address

On the Pi:
```sh
hostname -I | awk '{print $1}'
```
Write it down. You'll need it for the settings page on a phone, and to get back in if
`chatbox.local` stops resolving.

Home routers hand out addresses on a lease, so this can change after a reboot. To keep
it fixed, reserve it in your router: find "DHCP reservation" or "static lease" and pin
the Pi's hardware address (`ip link show wlan0`, the `link/ether` value) to one address.
The Pi itself needs no changes.

---

## 4. Update the system and install what Chatbox needs

```sh
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

Expect this to take 10–20 minutes on a Pi 3B the first time. Reconnect after the reboot.

Check the Python version:
```sh
python3 --version
```

Chatbox needs **3.11 or newer**. Raspberry Pi OS based on Debian 12 (Bookworm) ships
3.11, and Debian 13 (Trixie) ships 3.13. Both are fine. **Don't install a newer Python
yourself**: building Python on a Pi 3B is slow, and the system one is what the prebuilt
packages are published for. If it reports 3.9 or older, you're on an old image; rewrite
the card from step 1.

Install the system packages Chatbox uses:
```sh
sudo apt install -y git python3-venv libportaudio2
```

`libportaudio2` is the sound library `chatbox talk` records and plays through. Text chat
doesn't need it, but installing it now saves a step later.

**Verify:** `git --version` and `python3 -m venv --help` both print something.

---

## 5. Download and install Chatbox

```sh
cd ~
git clone https://github.com/adamhajari/chatbox.git
cd chatbox
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

The `.venv` (virtual environment) isn't optional. Raspberry Pi OS protects its own
Python, so `pip install` outside a venv fails with
`error: externally-managed-environment`. Make the venv; don't pass
`--break-system-packages`.

**What to expect from the install.** On a Pi 3B over Wi-Fi this takes roughly 5–10
minutes, nearly all of it downloading. Watch the lines as they scroll:

- `Downloading grpcio-…-manylinux_2_17_aarch64.whl (6.0 MB)`: good, that's the prebuilt
  package. The same goes for `pydantic_core-…aarch64.whl`.
- `Building wheel for grpcio (setup.py)`: **stop it with Ctrl-C.** pip found no prebuilt
  package and is about to compile gRPC, which on a Pi 3B takes hours and usually ends in
  `g++: fatal error: Killed signal terminated program cc1plus` when memory runs out. The
  cause is almost always a 32-bit image. Check with `uname -m`: it must print `aarch64`.
  If it prints `armv7l`, rewrite the card with the 64-bit image (step 1). That is faster
  than compiling.

**Verify:**
```sh
.venv/bin/python -c "import anthropic, grpc, pydantic; print('ok')"
```

---

## 6. Add your keys

**Nothing secret is ever committed.** `.env` is already in `.gitignore`, and the Google
key file lives outside the repo.

```sh
cd ~/chatbox
cp .env.example .env
nano .env          # Ctrl-O to save, Ctrl-X to quit
chmod 600 .env
```

Set `ANTHROPIC_API_KEY` to your key. That's all text chat needs.

For voice, you also need the Google service-account key. Create it on your computer by
following the README's [Voice setup](../README.md#voice-setup-for-chatbox-talk), then copy
it to the Pi:

```sh
# on the Pi
mkdir -p ~/.config/chatbox && chmod 700 ~/.config/chatbox

# on your computer
scp ~/.config/chatbox/gcp-speech.json <user>@chatbox.local:~/.config/chatbox/

# back on the Pi
chmod 600 ~/.config/chatbox/gcp-speech.json
```

and add these two lines to `.env`:
```
GOOGLE_APPLICATION_CREDENTIALS=/home/<user>/.config/chatbox/gcp-speech.json
GOOGLE_CLOUD_PROJECT=your-project-id
```
Use the full path, not `~`. It's read as a literal path, and `~` silently fails.

**Verify:**
```sh
ls -l .env ~/.config/chatbox/          # both should show -rw------- (600)
git status --short                     # must NOT list .env or any key file
```

---

## 7. Try text chat

All commands run from `~/chatbox` with the venv's Python. `.venv/bin/chatbox` is the
installed command; run `source .venv/bin/activate` first if you'd rather type `chatbox`.

```sh
cd ~/chatbox
.venv/bin/chatbox check-policy      # OK: …/policies/default.yaml (version …)
.venv/bin/chatbox chat -v           # ask a question; -v prints each step
```

Next, give the Pi more time for the safety check. Its default 4 s deadline is too tight
for a Pi 3B: questions time out and get the "something went wrong" reply. Create
`chatbox.local.toml`, which holds this Pi's own settings:

```sh
cp chatbox.local.toml.example chatbox.local.toml
nano chatbox.local.toml
```

and uncomment `timeout_seconds = 9` under `[guardrails.classifier]`.

**Common failures**

| What you see | What it means |
|---|---|
| `Config file not found: chatbox.toml` | you're not in `~/chatbox`; `cd` there first |
| `Policy file not found: …` | same |
| `Could not resolve authentication method` / 401 from the API | `ANTHROPIC_API_KEY` is missing or wrong in `.env` |
| every answer is the "something went wrong" reply | the Pi can't reach the API, or the deadline above is still 4 s. Check `ping -c3 api.anthropic.com`, and that the clock is right (`date`), since a wrong clock breaks secure connections |

---

## 8. Add the hardware and talk to it

Wire and set up each part by following [hardware.md](hardware.md): the button and light,
then the amplifier and speaker (including its **Enabling it** and **Volume** steps), then
the microphone. Each part there has its own check to run before moving on.

Then tell Chatbox about them in `chatbox.local.toml`. Uncomment the `[voice]` lines for
the button and light, and add the audio devices:

```toml
[voice]
button_gpio = 17
led_gpio = [22, 23, 24]
led_common_anode = true
input_device = "default"
output_device = "default"
```

Now start it:

```sh
.venv/bin/chatbox talk -v
```

Hold the button, ask a question, let go. The light goes green while listening, amber
while thinking, and blue while speaking.

| What you see | What it means |
|---|---|
| "no microphone or speaker was found", or "no sound devices are available" | the amplifier or mic isn't set up yet; go back to hardware.md |
| `Invalid sample rate` | `input_device` / `output_device` aren't set to `"default"` |
| it always says it didn't catch that | the mic is too quiet; see the microphone's **Level** steps in hardware.md |
| an error mentioning Google credentials or the project | the key path or project ID in `.env` is wrong (step 6) |

---

## 9. Open the settings page on your phone

```sh
cd ~/chatbox
.venv/bin/chatbox talk --web        # or: chat --web
```

It prints the address to open:
```
Parent settings: open http://192.168.x.y:8321/ on a phone or computer on this network.
```

Open that on a phone on the same Wi-Fi. Change a topic and save; the next question
follows the new rules without a restart.

Two things to know:

- **The page has no password.** Anyone on your home network can change what Chatbox will
  talk about. Requests from outside your local network are refused, but that's all.
- **Nothing starts at boot.** You SSH in and start Chatbox by hand, and closing the SSH
  session stops it. To keep it running after you disconnect, start it inside `tmux`:
  ```sh
  sudo apt install -y tmux
  tmux new -s chatbox           # run chatbox in here; Ctrl-B then D to detach
  tmux attach -t chatbox        # come back to it
  ```

**If the phone can't reach it:** check the phone is on the same Wi-Fi (not cellular, and
not a guest network: guest networks block devices from reaching each other, which is the
usual cause). `curl -sI http://127.0.0.1:8321/` on the Pi tells you whether the server
is up or the network is in the way.

---

## Updating

```sh
cd ~/chatbox
git pull
.venv/bin/pip install -e .
```

Your `.env`, `chatbox.local.toml` and conversation data aren't part of the repo, so
updating never touches them.
