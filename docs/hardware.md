# Talkbox hardware wiring

The button and status light for the Raspberry Pi build, as wired and verified on
2026-09-22. The microphone, speaker and MAX98357A amplifier aren't wired yet; the pin
choices below leave room for them.

For getting the software onto the Pi in the first place, see
[pi-setup.md](pi-setup.md).

---

## Pinout

Physical pin numbers are the ones you count on the header; GPIO numbers are what the
code uses. They are not the same, and `gpiozero` wants the GPIO number.

| Signal | GPIO | Physical pin | Goes to |
|---|---|---|---|
| Button | GPIO17 | 11 | arcade button **C** (common) |
| Button return | — | 9 (GND) | arcade button **NO** (normally open) |
| LED red | GPIO22 | 15 | 220 Ω → red leg |
| LED green | GPIO23 | 16 | 220 Ω → green leg |
| LED blue | GPIO24 | 18 | 220 Ω → blue leg |
| LED common | — | 1 (3V3) | long leg, **no resistor** |

**Reserved — don't use for anything else.** GPIO18, GPIO19 and GPIO21 are the I2S lines
(BCK, LRCLK, DIN) the MAX98357A amplifier will need. Everything above was chosen to keep
them free so the button and LED never have to be rewired.

---

## The LED

A 5 mm **common-anode** RGB LED: the long leg goes to **3V3**, and a GPIO pulled **low**
lights that colour. (Common-cathode parts exist and work the other way round — long leg
to GND, GPIO high to light. Check yours before wiring: put 220 Ω from 3V3 to a short leg
and touch the long leg to GND. If it lights, it's common cathode.)

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

## Assembling it

Verified on a breadboard first (`scripts/gpio_check.py`), then soldered. The rule
throughout: **solder at the components, never at the Pi's header** — the header is the
only way to take the thing apart again.

**Wires.** Cut female-to-female DuPont jumpers in half and solder the cut end to the
component. That leaves a proper female connector at the Pi end with no crimp tool
involved, and the whole assembly unplugs. Five wires: two for the button, four for the
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

---

## Still to do

- `src/talkbox/audio/pi.py`: the Pi adapter — button as push-to-talk in place of the
  laptop's spacebar, the LED driven from the pipeline's states, USB microphone capture and
  I2S output. Needs the mic and speaker in hand, since `VoiceTurn` wants an audio iterable
  and a `Speaker`.
- MAX98357A on GPIO18/19/21, plus the device-tree overlay in `/boot/firmware/config.txt`
  and `libportaudio2`.
- Enclosure: cardboard first, 3D printed later (PLAN.md D14). The 28.5 mm button hole and
  a 5 mm LED hole are the only ones the front panel needs so far.
