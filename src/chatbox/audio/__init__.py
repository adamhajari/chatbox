"""Input/output adapters (PLAN.md section 7). The core (`chatbox.voice`) sees only an
audio iterable and a `Speaker`; each device supplies its own mic, speaker and button."""


def audio_device_problem(input_device=None, output_device=None) -> str | None:
    """A plain-English reason `chatbox talk` can't run here, or None if audio looks usable.

    Checked before anything else in `chatbox talk` so a machine with no sound card (a
    fresh Raspberry Pi, PLAN.md phase 6) gets a sentence instead of a PortAudio stack
    trace. Nothing is opened; only the device list is read.
    """
    try:
        import sounddevice as sd
    except OSError as e:  # PortAudio itself is missing (RPi OS Lite has no libportaudio2)
        return (f"the PortAudio sound library isn't installed ({e}). "
                "On Raspberry Pi OS / Debian: sudo apt install -y libportaudio2")
    except ImportError as e:
        return f"the sounddevice package isn't installed ({e}). Reinstall Chatbox: pip install -e ."

    try:
        devices = sd.query_devices()
    except Exception as e:  # PortAudioError and friends: no sound card at all
        return (f"no sound devices are available ({e}). Plug in a microphone and speaker, "
                "then try again.")

    has_in = any(d["max_input_channels"] > 0 for d in devices)
    has_out = any(d["max_output_channels"] > 0 for d in devices)
    if not has_in and not has_out:
        return ("no microphone or speaker was found. Plug them in, check them with "
                "`python -m sounddevice`, and try again.")
    if not has_in:
        return ("no microphone was found (there is a speaker, but nothing to record with). "
                "Plug in a USB microphone, check it with `python -m sounddevice`, and try again.")
    if not has_out:
        return ("no speaker was found (there is a microphone, but nothing to play through). "
                "Plug in a speaker, check it with `python -m sounddevice`, and try again.")

    # A name from chatbox.local.toml that matches nothing is the likely mistake here, and
    # PortAudio's own failure for it surfaces much later as an empty recording.
    for device, kind in ((input_device, "input"), (output_device, "output")):
        if device is None:
            continue
        try:
            sd.query_devices(device, kind)
        except Exception:
            channels = f"max_{kind}_channels"
            available = [d["name"] for d in devices if d[channels] > 0]
            return (f"no {kind} device matches {device!r}, which is set as "
                    f"{kind}_device in chatbox.toml or chatbox.local.toml.\n"
                    f"Available {kind}s: {', '.join(repr(n) for n in available) or 'none'}.\n"
                    f"Names match on any part of the name; remove the setting to use the "
                    f"system default.")
    return None
