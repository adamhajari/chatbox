#!/usr/bin/env python3
"""Check a microphone: does the Pi see it, is it loud enough, and can speech-to-text
read what was said?

Needs no speaker, so it works before the amplifier and speaker are wired. Chatbox's
own `chatbox talk` needs both, by design.

    .venv/bin/python scripts/mic_check.py                 # list devices, record 5 s, show levels
    .venv/bin/python scripts/mic_check.py -s 8            # record for 8 seconds
    .venv/bin/python scripts/mic_check.py --device 1      # a specific input
    .venv/bin/python scripts/mic_check.py --transcribe    # also send it to speech-to-text
    .venv/bin/python scripts/mic_check.py --save kid.wav  # keep the recording
    .venv/bin/python scripts/mic_check.py --playback      # play it back (headphones/speaker)

`--transcribe` needs the Google credentials in .env (see docs/pi-setup.md, step 6) and
costs a fraction of a cent. Recordings are kept only with --save.
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RATE = 16_000          # what the voice pipeline uses
CHUNK = RATE // 10     # 0.1 s, the size the push-to-talk adapter yields


def levels(pcm: bytes) -> tuple[float, int]:
    """RMS and peak of 16-bit mono PCM, the same measure chatbox.voice uses."""
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0, 0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return rms, max(abs(s) for s in samples)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--seconds", type=float, default=5.0)
    ap.add_argument("--device", help="input device name or number (default: system default)")
    ap.add_argument("--transcribe", action="store_true", help="send it to speech-to-text")
    ap.add_argument("--save", type=Path, help="write the recording to this .wav")
    ap.add_argument("--playback", action="store_true",
                    help="play the recording back, to check audio out as well as in")
    args = ap.parse_args()

    try:
        import sounddevice as sd
    except OSError as e:
        sys.exit(f"PortAudio isn't installed ({e}).\n"
                 "On the Pi: sudo apt install -y libportaudio2")

    try:
        devices = sd.query_devices()
    except Exception as e:
        sys.exit(f"No sound devices at all ({e}). Is the mic plugged in?")

    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    if not inputs:
        sys.exit("No input devices. The Pi can't see a microphone — check `lsusb` and "
                 "`arecord -l`.")

    print("Input devices:")
    for i, d in inputs:
        print(f"  [{i}] {d['name']}  ({d['max_input_channels']} ch, "
              f"default rate {d['default_samplerate']:.0f} Hz)")

    device = args.device
    if device is not None and device.isdigit():
        device = int(device)

    print(f"\nRecording {args.seconds:.0f} s at {RATE} Hz. Talk normally, from where a "
          "child would stand.")
    # RawInputStream rather than sd.rec(), which needs NumPy; Chatbox doesn't depend on it.
    captured: list[bytes] = []
    try:
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                               device=device, blocksize=CHUNK) as stream:
            for _ in range(int(args.seconds * RATE / CHUNK)):
                data, _overflowed = stream.read(CHUNK)
                captured.append(bytes(data))
    except Exception as e:
        sys.exit(f"Recording failed ({e}).\n"
                 "If it mentions the sample rate, the mic may not do 16 kHz natively; "
                 "try --device with a different input, or check `arecord -l`.")
    pcm = b"".join(captured)

    rms, peak = levels(pcm)
    print(f"\nlevel: RMS {rms:.0f}, peak {peak} (of 32767)")

    # The pipeline skips speech-to-text below this, to avoid paying for silence.
    threshold = 200.0
    try:
        import tomllib

        threshold = float(tomllib.loads((ROOT / "chatbox.toml").read_text())
                          .get("voice", {}).get("silence_rms", 200))
    except Exception:
        pass

    if rms < threshold:
        print(f"  TOO QUIET. Chatbox treats anything under RMS {threshold:g} as silence "
              "and replies \"didn't catch that\" without calling speech-to-text.\n"
              "  Move closer, or raise the mic's gain: `alsamixer -c <card>`, F4 for "
              "capture, arrow up.")
    elif peak > 32000:
        print("  CLIPPING. The peak is at the ceiling, which distorts speech and hurts "
              "recognition. Lower the capture gain in `alsamixer`.")
    else:
        print(f"  Good: comfortably above the RMS {threshold:g} silence threshold, "
              "and not clipping.")

    if args.playback:
        outputs = [(i, d) for i, d in enumerate(devices) if d["max_output_channels"] > 0]
        if not outputs:
            print("\nNo output device, so nothing to play through. Plug in headphones or "
                  "a speaker.")
        else:
            print("\nOutput devices:")
            for i, d in outputs:
                print(f"  [{i}] {d['name']}")
            print("Playing it back…")
            try:
                with sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16") as out:
                    out.write(pcm)
            except Exception as e:
                print(f"  Playback failed ({e}).\n"
                      "  On the Pi, check the headphone jack is the default output: "
                      "`sudo raspi-config` → System Options → Audio. Volume: `alsamixer`.")
            else:
                print("  If you heard nothing, it's the volume or the wrong output: "
                      "`alsamixer` (F6 picks the card), or raspi-config as above.")

    if args.save:
        with wave.open(str(args.save), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        print(f"\nsaved {args.save} ({len(pcm) / 2 / RATE:.1f} s)")

    if not args.transcribe:
        print("\nAdd --transcribe to send this to speech-to-text.")
        return

    from dotenv import load_dotenv

    from chatbox.config import load_settings
    from chatbox.speech import SpeechError, make_stt

    load_dotenv(ROOT / ".env")
    sp = load_settings(ROOT / "chatbox.toml").speech
    if sp is None:
        sys.exit("chatbox.toml has no [speech] section.")

    print(f"\nTranscribing with {sp.stt_name}…")
    chunks = [pcm[i:i + CHUNK * 2] for i in range(0, len(pcm), CHUNK * 2)]
    try:
        stt = make_stt(sp.stt_name, sp.stt_settings)
        result = stt.transcribe(chunks, RATE)
    except SpeechError as e:
        sys.exit(f"Speech-to-text failed: {e}\n"
                 "Check GOOGLE_APPLICATION_CREDENTIALS and GOOGLE_CLOUD_PROJECT in .env "
                 "(docs/pi-setup.md, step 6).")

    conf = "" if result.confidence is None else f"  (confidence {result.confidence:.2f})"
    print(f"heard: {result.text!r}{conf}")
    if not result.text.strip():
        print("  Nothing recognized. Loud enough but unintelligible usually means the mic "
              "is too far away, or the room is too echoey.")


if __name__ == "__main__":
    main()
