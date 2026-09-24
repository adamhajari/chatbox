# Chatbox hardware wiring

The button, status light, amplifier and speaker, microphone and picture screen for the
Raspberry Pi build. The button and LED were wired and verified on 2026-09-22, and the
amplifier, speaker and USB microphone on 2026-09-23. The screen's pinout was chosen on
2026-09-23 and is **not yet verified on the bench** (see "Verifying it").

For getting the software onto the Pi in the first place, see
[pi-setup.md](pi-setup.md).

---

## Parts

| Component | Part | Connects by |
|---|---|---|
| Computer | Raspberry Pi 3 Model B v1.2 (1 GB) | — |
| Power supply | 5 V, 2.5 A micro-USB | micro-USB |
| Push-to-talk button | Philmore 30-781 SPDT arcade button, 28.5 mm hole | GPIO |
| Status light | 5 mm common-anode RGB LED, with three 220 Ω resistors | GPIO |
| Amplifier | NULLLAB MAX98357A I2S amplifier, from the amp & speaker kit | GPIO (I2S) |
| Speaker | the kit's own small 4 Ω, 3 W speaker | amplifier speaker output |
| Microphone | SuziePi USB 2.0 mini microphone | USB |
| Picture screen | 2.2" 240×320 SPI TFT, ILI9341 controller (red PCB) | GPIO (SPI) |

Wiring is female-to-female DuPont jumpers, cut and soldered at the component end (see
"Assembling it"), plus 2.8 mm push-on spade connectors for the button. The screen may
also need a P-MOSFET to switch its backlight, depending on the current it draws (see
"The screen").

---

## Pinout

Physical pin numbers are the ones you count on the header; GPIO numbers are what the
code uses. They are not the same, and `gpiozero` wants the GPIO number.

![Raspberry Pi 40-pin GPIO header, showing each physical pin number and its GPIO number or function](images/pi-gpio-header.png)

*The 40-pin header. The board drawn is a Pi 4, but the header is the same on the Pi 3B.
Image: [Raspberry Pi documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio),
© Raspberry Pi Ltd, [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).*

| Signal | GPIO | Physical pin | Goes to |
|---|---|---|---|
| Button | GPIO17 | 11 | arcade button **C** (common) |
| Button return | — | 9 (GND) | arcade button **NO** (normally open) |
| LED red | GPIO22 | 15 | 220 Ω → red leg |
| LED green | GPIO23 | 16 | 220 Ω → green leg |
| LED blue | GPIO24 | 18 | 220 Ω → blue leg |
| LED common | — | 1 (3V3) | long leg, **no resistor** |
| Amp VIN | — | 2 (5V) | MAX98357A VIN, **5 V, not 3.3 V** |
| Amp GND | — | 6 (GND) | MAX98357A GND |
| Amp BCLK | GPIO18 | 12 | I2S bit clock |
| Amp LRC | GPIO19 | 35 | I2S word select |
| Amp DIN | GPIO21 | 40 | I2S data |
| Screen VCC | — | 17 (3V3) | **3.3 V, not 5 V** |
| Screen GND | — | 20 (GND) | |
| Screen CS | GPIO8 (CE0) | 24 | hardware SPI0 |
| Screen SCK | GPIO11 | 23 | hardware SPI0 |
| Screen SDI (MOSI) | GPIO10 | 19 | hardware SPI0 |
| Screen SDO (MISO) | — | — | leave unconnected: nothing is read back |
| Screen D/C | GPIO25 | 22 | |
| Screen RESET | GPIO27 | 13 | |
| Screen LED | GPIO12 | 32 | backlight, switched (see below) |

**The backlight is on a GPIO**, not on 3V3. Filling the panel black still leaves a lit
grey rectangle, which is no good in a bedroom, and there are only two 3V3 pins (1 and
17) for three things that want 3.3 V. GPIO12 also solves that: pin 1 keeps the status
LED's common leg and pin 17 takes the screen's VCC.

GPIO12 was chosen over the other free pins because it is one of the two hardware-PWM
pins that don't collide with the amplifier's I2S lines (the other is GPIO13), so
dimming or fading the backlight stays possible later. GPIO7 (pin 26) would have been
tidier to wire, but it is SPI0's CE1 and the default SPI overlay claims it; freeing it
needs `dtoverlay=spi0-1cs` and gives up hardware PWM.

**GPIO18, GPIO19 and GPIO21 belong to the amplifier's I2S bus** — don't reuse them. The
button, LED and screen pins were chosen to keep them free. Note that GPIO19 and GPIO21
are physical pins 35 and 40, *not* 19 and 21, which are the screen's SPI lines.

The microphone is USB, so it takes no header pins.

---

## The LED

A 5 mm **common-anode** RGB LED: the long leg goes to **3V3**, and a GPIO pulled **low**
lights that colour. (Common-cathode parts exist and work the other way round — long leg
to GND, GPIO high to light. Check yours before wiring: put 220 Ω from 3V3 to a short leg
and touch the long leg to GND. If it lights, it's common cathode.)

![RGB LED wiring: the common anode leg straight to 3V3 (pin 1); red, green and blue each through 220 Ω to GPIO22, GPIO23 and GPIO24 (pins 15, 16, 18)](images/rgb-led.svg)

`gpiozero` handles the inversion with `active_high=False`, so the only difference in code
is that one flag. **The adapter has to carry it as a setting**, not a constant — a
replacement LED may well be the other type.

**One 220 Ω resistor per colour leg, none on the common leg.** A single resistor on the
shared common leg would dim every colour whenever a second one came on, so "thinking"
(red + green) would be dimmer than "listening" (green alone) — the opposite of a signal a
child can read at a glance.

220 Ω on all three was measured bright enough on this LED at 3.3 V. That isn't guaranteed
for every part: green and blue have a forward voltage near 3 V, so with only 0.3 V across
the resistor they can come out dim or dark. If a future LED looks red-dominated, drop
green and blue to about 100 Ω. Don't raise the supply to 5 V to fix brightness — with the
GPIO low that pushes 5 V through the resistor into a 3.3 V pin.

### States

| State | Colour | Channels |
|---|---|---|
| listening | green | G |
| thinking | amber | R + G |
| speaking | blue | B |
| idle | off | — |

---

## The button

A Philmore SPDT arcade button, 28.5 mm hole. Only two of its three terminals are used:
**C** to GPIO17 and **NO** to ground. **NC** stays unconnected. The button's own lamp
isn't used — the RGB LED above is the status light.

No pull-up resistor: `Button(17, pull_up=True)` enables the Pi's internal one, so the pin
idles high and reads low when pressed. `bounce_time=0.05` absorbs the contact bounce an
arcade microswitch produces, which matters because push-to-talk decides from how long the
button is *held*.

---

## The amplifier and speaker

A MAX98357A I2S amplifier driving the small 4 Ω, 3 W speaker that came in the same kit
(PLAN.md D24). Wire it by the
**physical** pin numbers below: GPIO19 and GPIO21 are *not* physical pins 19 and 21 —
those two are SPI lines reserved for the screen, and confusing them is the easiest
mistake to make on this build.

| Amp pin | Physical pin | GPIO | What it is |
|---|---|---|---|
| VIN | 2 | — | 5 V, not 3.3 V: a 4 Ω speaker wants the power |
| GND | 6 | — | |
| BCLK | 12 | GPIO18 | bit clock |
| LRC / LRCLK | 35 | GPIO19 | left-right (word select) clock |
| DIN | 40 | GPIO21 | serial data in |
| GAIN | — | — | leave unconnected for the 9 dB default |
| SD | — | — | leave unconnected: on, and mono (L+R)/2 |

The speaker goes to the amplifier's speaker output. **Neither speaker wire goes to ground** —
the output is bridge-tied, and grounding one side shorts the amplifier.

### Enabling it

Two edits to `/boot/firmware/config.txt`, one changed line and one new one:

1. **Change** the existing `dtparam=audio=on` line, near the top of the file, to:

   ```
   dtparam=audio=off
   ```

2. **Add** this line at the end of the file, after the `[all]` line:

   ```
   dtoverlay=hifiberry-dac
   ```

Then reboot. **This disables the 3.5 mm headphone jack**, which is PWM-driven and gives
way to the I2S device. The headphones stop working the moment the speaker starts. To
get them back, set `dtparam=audio=on` again, comment out the `dtoverlay` line, and
reboot.

### Verifying it

```sh
aplay -l                        # expect a snd_rpi_hifiberry_dac card
speaker-test -c2 -t wav         # noise from the speaker
.venv/bin/python scripts/mic_check.py --playback
```

| What you see | What it means |
|---|---|
| no hifiberry card in `aplay -l` | the overlay didn't load — check `config.txt` and that you rebooted |
| card appears, no sound | check the speaker terminals, and `alsamixer` volume on the new card |
| a lightning bolt, or the Pi reboots when it gets loud | power, not the amp: the MAX98357A pulls over an amp in peaks at 5 V into 4 Ω. Use the 2.5 A supply |
| Chatbox plays through the wrong device | set `output_device` in `chatbox.local.toml` to a name from `scripts/mic_check.py` |

Getting a pin wrong here is the likely first failure, and it doesn't announce itself:
the card still enumerates, ALSA still accepts frames, and the result is silence or a
buzz. Check the **physical** numbers again before suspecting anything else.

### Volume

**The MAX98357A has no hardware volume control.** It is a plain I2S DAC, so until the
steps below are done, `alsamixer -c sndrpihifiberry` reports "This sound device does
not have any controls". That is correct, not a fault. Volume has to be done in software,
with ALSA's `softvol` plugin, which creates a real `Master` control that `alsamixer`,
`amixer` and Chatbox's settings page all share.

**1. Create `/etc/asound.conf`.** It doesn't exist on a fresh Pi. Open it with
`sudo nano /etc/asound.conf` and paste in exactly this:

```
pcm.!default {
    type         asym
    playback.pcm "softvol"
    capture.pcm  "capture_plug"
}

pcm.capture_plug {
    type      plug
    slave.pcm "hw:CARD=Device"
}

pcm.softvol {
    type      softvol
    slave.pcm "plughw:CARD=sndrpihifiberry"
    control {
        name  "Master"
        card  "sndrpihifiberry"
    }
    min_dB -51.0
    max_dB   0.0
}

ctl.!default {
    type hw
    card "sndrpihifiberry"
}
```

**2. Point Chatbox at the default device.** Add this to `chatbox.local.toml`:

```toml
[voice]
input_device = "default"
output_device = "default"
```

**3. Play something once.** The `Master` control doesn't exist until the device is
first opened, so it won't show up in `alsamixer` before this:

```sh
speaker-test -c2 -t wav -l1
```

**4. Set the volume and save it.** Levels reset at boot unless stored:

```sh
alsamixer                  # arrow keys to set Master, Esc to leave
sudo alsactl store
```

**5. Set the ceiling.** `max_dB` caps how loud the device can ever go, in the one place
a child or a stray `amixer` call can't override. With `max_dB` at 0, turn `Master` to
100% and hear what full scale sounds like in the room. Then lower `max_dB` in
`/etc/asound.conf` until 100% *is* the loudest you want it, and run
`sudo alsactl store` again. This is a hearing-safety setting on a box a small child
holds near their face.

#### Why it's set up this way

**`/etc/asound.conf` pins the default device**, which it has to: turning the onboard
audio off removes the card everything used to default to, so anything asking for "the
default" would otherwise land on HDMI or fail. It covers `aplay`, `speaker-test` and
PortAudio in one place. `pcm.!default` is an `asym` device: playback goes through
`softvol` to the amplifier, capture goes to the USB mic.

**Chatbox has to ask for `default` by name** (step 2). Otherwise it asks PortAudio for
*its* default, which is a raw `hw:` device that does no resampling. The mic can't do the
pipeline's 16 kHz and the DAC can't do the 24 kHz text-to-speech produces, so both
directions fail with `Invalid sample rate`. Going through `default` puts the `asym`
device and its `plug` conversion in the path.

**Cards are addressed by ID, never by number.** Card numbers are assigned in probe order
and can swap between boots. If `sndrpihifiberry` and the USB mic trade places, anything
addressed by number points capture at the amplifier and playback at the microphone.
`hw:CARD=Device` and `plughw:CARD=sndrpihifiberry` are stable; `cat /proc/asound/cards`
lists the IDs. Same for the tools: `alsamixer -c Device`, `amixer -c sndrpihifiberry`.

---

## The microphone

A plug-and-play USB mic: no driver, no wiring. Setting it up is plugging it in, checking
the Pi sees it, and setting its level.

**1. Plug it in and check the Pi sees it.**

```sh
cat /proc/asound/cards
```

Look for a card with the ID `Device` (the word in square brackets). That ID is how
everything refers to the mic. Ignore the card *number* in front of it: it changes
between boots.

**2. Make sure `/etc/asound.conf` is in place.** If you followed the amplifier's Volume
steps above, it already is and there's nothing more to configure. The `capture_plug`
section in that file is what makes the mic usable (see "Why" below).

**3. Check the level**, speaking from where a child will stand, not from where you are:

```sh
.venv/bin/python scripts/mic_check.py
```

It records five seconds and reports RMS and peak. Aim for:

- **RMS 300–1000** speaking normally. Below 200 (the `silence_rms` setting in
  `chatbox.toml`), Chatbox answers "didn't catch that" without ever calling
  speech-to-text, so a quiet mic looks like a broken assistant.
- **Peak under about 20000** when someone is excited. Clipping wrecks recognition far
  worse than a low level does, so err slightly quiet: the threshold is adjustable,
  clipping isn't recoverable.

**4. If it's off, adjust the capture level** and run step 3 again:

```sh
amixer -c Device scontrols     # list what this mic has
alsamixer -c Device            # F4 for the capture view, arrow keys to set, Esc to leave
```

**5. Save the level.** Unsaved changes revert at the next boot:

```sh
sudo alsactl store
```

Verified 2026-09-23 at capture 13/16 (81%, 19.34 dB): RMS 1510, peak 10981.

### If RMS and peak are exactly 0

In `alsamixer`'s capture view, **Space** toggles whether an item is a capture source and
**M** mutes it, and both are easy to hit while arrowing the level. Exactly 0 means
digital silence, not a quiet signal: even a badly placed mic picks up some noise. To fix
it:

```sh
amixer -c Device sget Mic      # look for [off]
amixer -c Device sset Mic cap  # turn capture back on
sudo alsactl store             # or the muted state comes back at the next boot
```

### Why it needs `capture_plug`

**The mic does not do 16 kHz**, which is the rate the voice pipeline uses. Opening it
directly (`hw:0,0`) fails with `Invalid sample rate [PaErrorCode -9997]`. It has to go
through ALSA's `plug` layer, which resamples: that's the `capture_plug` section in
`/etc/asound.conf`. `pcm.!default` is an `asym` device for the same reason, so playback
goes to the amplifier and capture goes to the mic. A `!default` that only names a
playback device leaves capture with nowhere to go.

---

## The screen

A 2.2" TFT, 240x320, SPI, on a red PCB marked `QVGA 2.2 TFT SPI 240*320` — an ILI9341
controller. **Confirm the controller before building on it**: `scripts/screen_check.py` drives it as an ILI9341,
and a panel that stays white while the backlight is on is the symptom of a different
controller (an ILI9325 or an ST7789 want a different driver).

No touch panel on this one, and the SD card socket on the back is unused — neither is
wired and neither is wanted.

**It is a 3.3 V part.** VCC goes to 3V3 (pin 17); 5 V will damage it.

**The backlight is switched from GPIO12**, not tied to 3V3 — see the pinout above for
why. Measure the backlight current before wiring it straight to the pin: a GPIO should
source no more than about 16 mA, and if the panel wants more than that it needs a
transistor between the pin and the backlight rather than a direct connection.

**MISO stays unconnected.** The driver only writes, and leaving it off keeps one more
pin free.

Pictures only, never text (D28): young children may not read yet, so the screen
supplements the spoken answer and is never needed to understand it.

**What it shows and when.** The picture goes up as the answer starts being spoken and
comes down when the turn ends; the rest of the time the screen is blank. Blank rather
than an idle picture, because a screen lit only while Chatbox speaks tells a child which
of the things on the front panel is the one talking — and it can't sit showing a
jellyfish half an hour after anyone asked about one.

---

## Assembling it

The button and LED were verified on a breadboard first (`scripts/gpio_check.py`), then
soldered. The rule
throughout: **solder at the components, never at the Pi's header** — the header is the
only way to take the thing apart again.

**Wires.** Cut female-to-female DuPont jumpers in half and solder the cut end to the
component. That leaves a proper female connector at the Pi end with no crimp tool
involved, and the whole assembly unplugs. Six wires: two for the button, four for the
LED.

**Before cutting anything**, mark the LED legs R / A / G / B with tape. The common leg is
identified by being longest, and trimming the legs destroys that cue permanently.

**Each LED leg**, in this order — the first step is the one that gets skipped:

1. Slide the heat-shrink onto the wire. It cannot be added afterwards.
2. Tin the LED leg and the resistor lead.
3. Solder the resistor to the leg, a few centimetres from the LED body so the heat
   doesn't reach the die.
4. Solder the wire to the resistor's far end.
5. Shrink the tubing over the joint, resistor included.

Leave the leads long enough to reach the front panel with slack. Cardboard means the
LED will get repositioned.

**The button** has 2.8 mm quick-connect spade lugs. Push-on spade connectors are better
than solder here: they survive repeated pressing, and this button will be hit thousands
of times by a child. If soldering instead, hook the wire through the lug's hole before
applying heat, and zip-tie the wire pair to the button body for strain relief so the
joint never takes the pull.

**Label every wire** with tape as you go. Four near-identical wires from an RGB LED are
miserable to trace later.

**The screen** needs no soldering: its header takes female DuPont jumpers directly.
Eight wires to the Pi (VCC, GND, CS, SCK, MOSI, D/C, RESET, LED), and **MISO left off**.
If the current measurement says the backlight needs the P-MOSFET, that goes in the LED
wire and is the one part of the screen that does need soldering. Keep them
short — long SPI leads are the usual cause of a noisy picture at 24 MHz. The panel is
mounted **landscape**, 320 wide by 240 tall (`rotation = 90`), because a photograph of a
thing is usually wider than it is tall and a portrait panel letterboxes most lead images
heavily. If it ends up the other way round in the enclosure, set `rotation` in
`chatbox.local.toml` rather than rewiring — 90 and 270 are landscape, 0 and 180 portrait.
Pictures are sized to match, and the cache is keyed by that size, so changing it refetches
rather than stretching what is already there.

---

## Software

`gpiozero` is BSD-3-Clause and `lgpio` is Unlicense, both fine under the licensing
constraint in PLAN.md section 8a.

```sh
sudo apt install -y python3-dev swig liblgpio-dev
.venv/bin/pip install gpiozero lgpio
```

All three apt packages are needed, and none ship in Raspberry Pi OS Lite. `lgpio`
publishes aarch64 wheels only up to cp39, so on Python 3.13 pip builds it from source:
that needs `swig` to generate the binding, `python3-dev` to compile it, and
`liblgpio-dev` to link against. Miss one and you get a different error each time:

| Error | Missing |
|---|---|
| `command 'swig' failed: No such file or directory` | `swig` |
| `fatal error: Python.h: No such file or directory` | `python3-dev` |
| `/usr/bin/ld: cannot find -llgpio` | `liblgpio-dev` |

pip builds every wheel before installing any of them, so one failure installs nothing —
retry the whole command, not just the part that failed.

The screen needs SPI turned on and two more packages, both Pi-only:

```sh
sudo raspi-config nonint do_spi 0    # 0 means ENABLE; the menu route is
sudo reboot                          # Interface Options -> SPI -> Yes
ls /dev/spidev*                      # expect spidev0.0 AND spidev0.1
.venv/bin/pip install adafruit-blinka adafruit-circuitpython-rgb-display
```

`/dev/spidev*` not existing is what SPI-still-off looks like, and it is the normal
state of a fresh Pi — it says nothing about the wiring. The device nodes appear only
after a reboot, so enabling SPI without rebooting looks identical to not enabling it.
`sudo raspi-config nonint get_spi` answers 1 for off and 0 for on.

Two nodes is correct: the default overlay claims both chip selects. We use CE0, and
`spidev0.1` sits unused (we deliberately left GPIO7 free by putting the backlight on
GPIO12 instead).

If they still don't appear after a reboot, check that `dtparam=spi=on` is present and
uncommented in **`/boot/firmware/config.txt`** — on Bookworm that is the file that is
read, and an edit to the older `/boot/config.txt` does nothing.

`adafruit-blinka` and `adafruit-circuitpython-rgb-display` are MIT, and Pillow (which
Chatbox installs everywhere, to scale the picture) is MIT-CMU — all fine under the
licensing constraint in PLAN.md section 8a. Both are imported lazily, so a laptop that
never enables the screen never needs them.

**Without `lgpio`**, `gpiozero` falls back to a pure-Python backend that works on a Pi 3B
for a button and an on/off LED. Fine for testing, but `lgpio` is the backend the Pi
Foundation maintains and the one that still works on a Pi 5.

---

## Verifying it

```sh
.venv/bin/python scripts/gpio_check.py --common-anode
```

Three passes: each colour named as it lights, then the four states, then a press-and-hold
loop printing how long the button was held.

| What you see | What it means |
|---|---|
| LED dark throughout | wrong polarity — drop or add `--common-anode` |
| colour names don't match what lights | swap the wires, or pass `--red/--green/--blue` with the GPIO numbers that match |
| `BadPinFactory` / no pin factory | no backend — install `liblgpio-dev` and reinstall `lgpio` |
| `Failed to add edge detection` or a permissions error | `sudo usermod -aG gpio $USER`, then log out and back in |

Verified 2026-09-22 on a Pi 3B: defaults matched with no swaps, all three colours bright
through 220 Ω.

### The screen

```sh
.venv/bin/python scripts/screen_check.py --backlight 12
.venv/bin/python scripts/screen_check.py --backlight 12 --backlight-active-low
.venv/bin/python scripts/screen_check.py --baudrate 16000000 --rotation 180
```

Four passes with no text in any of them: solid colours named as they come up, then a
border with crossed diagonals and a blue square marking the top left, then a colour
ramp, then — with `--backlight` — the backlight blinking three times behind a white
screen.
`--image some-photo.jpg` also shows a real photo, scaled and centred the way an answer
will be.

| What you see | What it means |
|---|---|
| nothing at all, backlight dark | the LED pin isn't on 3V3 — check VCC and LED both reach 3V3 and GND reaches a ground pin |
| backlight on, screen stays white | powered but not driven: D/C, RESET, MOSI or SCK wrong or loose, or SPI off (`ls /dev/spidev*` must list `spidev0.0`) |
| `No such file or directory: '/dev/spidev0.0'` | SPI isn't enabled — `raspi-config` → Interface Options → SPI, then reboot |
| `ImportError` on `board` / `busio` | Blinka isn't installed (see Software above) |
| permission denied on `/dev/spidev0.0` | `sudo usermod -aG spi $USER`, then log out and back in |
| colours named wrong (red shows blue) | an ILI9341 clone with BGR order — harmless for photos, but say so and the driver gets a flag |
| noise, torn lines, flicker | SPI too fast or leads too long — `--baudrate 16000000` |
| the blue square isn't top left | the panel is mounted the other way up — find the `--rotation` that looks right and put it in `chatbox.local.toml` |
| a white flash then nothing | RESET is floating — check GPIO27 |
| the backlight never goes dark | wrong polarity — add `--backlight-active-low` |
| the backlight never comes back on | GPIO12 can't supply the current — it needs the P-MOSFET switch above |
| the backlight is on before Chatbox starts | with a MOSFET, the gate pull-up is too weak for GPIO12's idle-low default — use 10 kΩ |

Once the panel itself is proved, `scripts/screen_demo.py` takes it the rest of the way —
the real classifier, a real Wikipedia lookup and the real display, with the microphone
and speaker left out so a screen problem can't hide behind an audio one:

```sh
.venv/bin/python scripts/screen_demo.py --loop
.venv/bin/python scripts/screen_demo.py "how do octopuses change colour?"
.venv/bin/python scripts/screen_demo.py --subject octopus     # skips the model call
```

Then `chatbox chat -v` drives the screen from real questions typed at the keyboard,
once `[screen] enabled = true` is in `chatbox.local.toml`. The picture appears a moment
after the answer, since nothing waits for it and there is no speech to cover the gap.

**Not yet run on the bench.** The pinout above is the intended wiring (PLAN.md D25
reserved SPI0 plus GPIO25/27 for exactly this), and it is what the script and
`chatbox.toml` default to, but nothing has been connected yet. Run this before
`chatbox talk`, and correct this table and the pinout if the panel disagrees.

---

## Still to do

- Measure the backlight current, wire the screen (with the P-MOSFET if the measurement
  calls for it), run `scripts/screen_check.py --backlight 12`, then turn it on with
  `[screen] enabled = true` and `backlight_gpio = 12` in `chatbox.local.toml`.
- Enclosure: cardboard first, 3D printed later (PLAN.md D14). The 28.5 mm button hole and
  a 5 mm LED hole are the only ones the front panel needs so far, plus a window for the
  screen: the visible area is about 34 x 45 mm, on a board about 40 x 62 mm.