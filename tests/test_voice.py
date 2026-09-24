"""Voice turn tests: speech services, microphone and speaker are all fakes."""

import time
from array import array

import pytest

from chatbox.audio.cues import cue_pcm
from chatbox.audio.laptop import HoldDetector
from chatbox.config import load_settings
from chatbox.guardrails import Classification, OutputVerdict
from chatbox.pipeline import Pipeline
from chatbox.speech import SpeechError, Transcript, make_stt, make_tts
from chatbox.speech.google import GoogleSpeechToText, GoogleTextToSpeech
from chatbox.voice import VoiceSettings, VoiceTurn
from tests.conftest import ROOT, WED_NOON, AllowAll, FakeProvider, PassAll

RATE = 16_000


def speech(seconds=1.0, level=3000):
    """Loud-enough PCM in 0.1 s chunks."""
    chunk = array("h", [level, -level] * (RATE // 20)).tobytes()
    return [chunk] * round(seconds * 10)


def silence(seconds=1.0):
    return [bytes(RATE // 10 * 2)] * round(seconds * 10)


class Events:
    """Shared, ordered record of what happened, to check nothing plays too early."""

    def __init__(self):
        self.items = []

    def add(self, what):
        self.items.append(what)

    def index(self, what):
        return self.items.index(what)


class FakeSTT:
    name, model = "fake-stt", "fake"

    def __init__(self, text="Why is the sky blue?", error=None, delay=0.0, fail_early=False):
        self.text, self.error, self.delay, self.fail_early = text, error, delay, fail_early
        self.received = 0

    def transcribe(self, audio, sample_rate):
        if self.fail_early:
            raise SpeechError("connection reset")
        for chunk in audio:
            self.received += len(chunk)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise SpeechError(self.error)
        return Transcript(self.text, 0.9)


class FakeTTS:
    name, voice, sample_rate = "fake-tts", "fake-voice", 24_000

    def __init__(self, events, error=None, error_after_first=False, empty=False):
        self.events, self.error = events, error
        self.error_after_first, self.empty = error_after_first, empty
        self.texts = []

    def synthesize(self, text):
        self.texts.append(text)
        self.events.add("tts_start")
        if self.error and not self.error_after_first:
            raise SpeechError(self.error)
        if self.empty:
            return
        yield b"\x01\x00" * 100
        if self.error_after_first:
            raise SpeechError(self.error)
        yield b"\x02\x00" * 100


class FakeSpeaker:
    def __init__(self, events):
        self.events = events
        self.cues, self.played = [], []

    def cue(self, name):
        self.cues.append(name)
        self.events.add(f"cue:{name}")

    def play(self, audio, sample_rate):
        for chunk in audio:
            self.events.add("audio")
            self.played.append(chunk)


class RecordingChecker:
    def __init__(self, events, passed=True, error=None):
        self.events, self.passed, self.error = events, passed, error

    def check(self, question, answer, history, policy):
        self.events.add("check_start")
        time.sleep(0.05)
        self.events.add("check_done")
        if self.error:
            raise self.error
        return OutputVerdict(self.passed, "test")


class Redirect:
    def classify(self, question, history, policy):
        return Classification("redirect", topic_id=policy.topics.redirect_to_parent[0].id,
                              detail="test")


@pytest.fixture
def events():
    return Events()


@pytest.fixture
def make(policy, counter, events):
    def _make(stt=None, tts=None, classifier=None, checker=None, provider=None, **settings):
        pipeline = Pipeline(policy, provider or FakeProvider(), counter,
                            classifier=classifier or AllowAll(),
                            output_checker=checker if checker is not None else PassAll(),
                            clock=lambda: WED_NOON)
        speaker = FakeSpeaker(events)
        tts = tts or FakeTTS(events)
        turn = VoiceTurn(pipeline, stt or FakeSTT(), tts, speaker, VoiceSettings(**settings))
        return turn, speaker, tts
    return _make


def steps(result):
    return [(s["step"], s["decision"]) for s in result.steps]


def test_recording_to_spoken_answer(make, events):
    stt = FakeSTT()
    turn, speaker, tts = make(stt=stt, checker=RecordingChecker(events))
    r = turn.run(speech(1.0), RATE)
    assert r.transcript == "Why is the sky blue?"
    assert r.spoken == r.answer.text == "The sky is blue because of sunlight."
    assert tts.texts == [r.spoken]
    assert speaker.played and speaker.cues == []
    assert stt.received == RATE * 2  # all audio streamed through
    assert steps(r) == [("stt", "ok"), ("limits", "allow"), ("classify", "allow"),
                        ("canned", "skip"), ("generate", "ok"), ("output_check", "pass"),
                        ("pipeline", "model"), ("tts", "ok"), ("speech_start", "ok")]
    assert all(s["ms"] is not None and s["ms"] >= 0 for s in r.steps
               if s["step"] in {"stt", "pipeline", "tts", "speech_start"})
    assert r.speech_start_ms is not None and r.speech_start_ms >= 0


def test_nothing_plays_before_output_check_finishes(make, events):
    turn, _, _ = make(checker=RecordingChecker(events))
    turn.run(speech(), RATE)
    assert events.index("check_done") < events.index("tts_start") < events.index("audio")


def test_failed_output_check_speaks_canned_reply_not_answer(make, events, policy):
    turn, speaker, tts = make(checker=RecordingChecker(events, passed=False))
    r = turn.run(speech(), RATE)
    assert tts.texts == [policy.canned_replies.blocked_topic]
    assert r.spoken == policy.canned_replies.blocked_topic


def test_output_check_error_fails_closed_before_speaking(make, events, policy):
    turn, _, tts = make(checker=RecordingChecker(events, error=RuntimeError("boom")))
    turn.run(speech(), RATE)
    assert tts.texts == [policy.canned_replies.something_went_wrong]
    assert events.index("check_done") < events.index("tts_start")


def test_redirect_reply_is_spoken(make, policy):
    provider = FakeProvider()
    turn, _, tts = make(classifier=Redirect(), provider=provider)
    r = turn.run(speech(), RATE)
    assert tts.texts == [policy.topics.redirect_to_parent[0].reply]
    assert r.answer.answered_by == "canned"


def test_tap_too_short_plays_cancel_cue_only(make):
    stt = FakeSTT()
    turn, speaker, tts = make(stt=stt)
    r = turn.run(speech(0.1), RATE)
    assert steps(r) == [("stt", "too_short")]
    assert speaker.cues == ["cancel"] and tts.texts == [] and r.answer is None


def test_empty_recording_is_a_tap(make):
    turn, speaker, _ = make()
    r = turn.run([], RATE)
    assert steps(r) == [("stt", "too_short")] and speaker.cues == ["cancel"]


def test_silence_gets_didnt_catch_that_without_pipeline(make, policy):
    provider = FakeProvider()
    turn, _, tts = make(provider=provider)
    r = turn.run(silence(1.0), RATE)
    assert steps(r)[0] == ("stt", "silence")
    assert tts.texts == [policy.canned_replies.didnt_catch_that]
    assert provider.calls == [] and r.answer is None


def test_no_words_recognized_gets_didnt_catch_that(make, policy):
    provider = FakeProvider()
    turn, _, tts = make(stt=FakeSTT(text="  "), provider=provider)
    r = turn.run(speech(), RATE)
    assert steps(r)[0] == ("stt", "empty")
    assert tts.texts == [policy.canned_replies.didnt_catch_that] and provider.calls == []


def test_stt_error_speaks_something_went_wrong(make, policy):
    turn, _, tts = make(stt=FakeSTT(error="quota exceeded"))
    r = turn.run(speech(), RATE)
    assert steps(r)[0] == ("stt", "error") and "quota exceeded" in r.steps[0]["detail"]
    assert tts.texts == [policy.canned_replies.something_went_wrong]


def test_stt_failing_early_still_waits_for_release(make, policy):
    consumed = []

    def audio():
        for chunk in speech(1.0):
            consumed.append(chunk)
            yield chunk

    turn, _, tts = make(stt=FakeSTT(fail_early=True))
    r = turn.run(audio(), RATE)
    assert len(consumed) == 10  # recording ran to the end (the button release)
    assert steps(r)[0] == ("stt", "error")
    assert tts.texts == [policy.canned_replies.something_went_wrong]


def test_stt_that_stops_reading_audio_cannot_hang_the_turn(make, policy):
    class Stuck:
        name, model = "stuck", "x"

        def transcribe(self, audio, sample_rate):
            next(iter(audio))
            time.sleep(1.0)
            raise SpeechError("gave up")

    turn, _, tts = make(stt=Stuck(), stt_timeout_seconds=0.2)
    t = time.monotonic()
    r = turn.run(speech(), RATE)
    assert time.monotonic() - t < 0.9
    assert steps(r)[0] == ("stt", "error")
    assert tts.texts == [policy.canned_replies.something_went_wrong]


def test_stt_timeout(make, policy):
    turn, _, tts = make(stt=FakeSTT(delay=0.5), stt_timeout_seconds=0.1)
    r = turn.run(speech(), RATE)
    assert steps(r)[0] == ("stt", "error") and "no transcript" in r.steps[0]["detail"]
    assert tts.texts == [policy.canned_replies.something_went_wrong]


@pytest.mark.parametrize("kw", [{"error": "unavailable"}, {"empty": True},
                                {"error": "reset", "error_after_first": True}])
def test_tts_failure_plays_error_cue(make, events, kw):
    turn, speaker, _ = make(tts=FakeTTS(events, **kw))
    r = turn.run(speech(), RATE)
    assert speaker.cues == ["error"]
    assert steps(r)[-1][0] == "tts" and steps(r)[-1][1] in {"error", "empty"}


def test_stt_and_tts_both_down_still_makes_a_sound(make, events):
    turn, speaker, _ = make(stt=FakeSTT(error="down"), tts=FakeTTS(events, error="down"))
    turn.run(speech(), RATE)
    assert speaker.cues == ["error"]


# ---- laptop adapter pieces that don't need devices -----------------------------------

def test_hold_detector_tap_vs_hold():
    tap = HoldDetector(repeat_wait=0.5, release_gap=0.1, now=0.0)
    assert not tap.released(0.4) and tap.released(0.6) and not tap.confirmed

    hold = HoldDetector(repeat_wait=0.5, release_gap=0.1, now=0.0)
    for t in (0.45, 0.5, 0.55, 0.6):
        hold.key(t)
        assert not hold.released(t + 0.05)
    assert hold.confirmed and hold.released(0.75)


def test_cues_are_short_pcm():
    for name in ("listening", "stopped", "cancel", "error"):
        pcm = cue_pcm(name, 24_000)
        assert 0 < len(pcm) / 2 / 24_000 < 0.5


# ---- settings and factories ----------------------------------------------------------

def test_speech_settings_load():
    sp = load_settings(ROOT / "chatbox.toml").speech
    assert sp.stt_name == "google" and sp.tts_name == "google"
    assert sp.stt_settings["language"] == sp.tts_settings["language"] == "en-US"
    assert {"min_press_seconds", "silence_rms", "stt_timeout_seconds"} <= set(sp.voice)


def test_unknown_speech_vendor():
    with pytest.raises(ValueError):
        make_stt("nope", {})
    with pytest.raises(ValueError):
        make_tts("nope", {})


def test_google_stt_streams_and_joins_finals(monkeypatch):
    from google.api_core import exceptions as gexc

    class Alt:
        def __init__(self, t, c):
            self.transcript, self.confidence = t, c

    class Res:
        def __init__(self, t, final):
            self.is_final, self.alternatives = final, [Alt(t, 0.8)]

    class Resp:
        def __init__(self, *results):
            self.results = list(results)

    class Client:
        error = None

        def streaming_recognize(self, requests, timeout):
            reqs = list(requests)
            self.first = reqs[0]
            self.audio = [r.audio for r in reqs[1:]]
            if self.error:
                raise self.error
            return [Resp(Res("why is", False)), Resp(Res("Why is the sky", True)),
                    Resp(Res("blue?", True))]

    client = Client()
    stt = GoogleSpeechToText(project="p", client=client, model="long")
    t = stt.transcribe([b"a" * 30_000, b"b" * 10], 16_000)
    assert t.text == "Why is the sky blue?" and t.confidence == 0.8
    assert client.first.recognizer == "projects/p/locations/global/recognizers/_"
    assert [len(a) for a in client.audio] == [25_600, 4_400, 10]  # split to the API limit

    client.error = gexc.ServiceUnavailable("down")
    with pytest.raises(SpeechError):
        stt.transcribe([b"a"], 16_000)


def test_google_stt_needs_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(SpeechError):
        GoogleSpeechToText(client=object())


def test_google_tts_yields_audio_and_wraps_errors():
    from google.api_core import exceptions as gexc

    class R:
        def __init__(self, a):
            self.audio_content = a

    class Client:
        error = None

        def streaming_synthesize(self, requests, timeout):
            self.requests = list(requests)
            if self.error:
                raise self.error
            return [R(b"x"), R(b""), R(b"y")]

    client = Client()
    tts = GoogleTextToSpeech(client=client)
    assert list(tts.synthesize("Hello")) == [b"x", b"y"]
    assert client.requests[1].input.text == "Hello"
    client.error = gexc.PermissionDenied("no")
    with pytest.raises(SpeechError):
        list(tts.synthesize("Hello"))


def test_google_tts_reads_stream_ahead_of_slow_playback():
    """Playback is slower than synthesis; the stream must not wait on the speaker."""
    import threading

    finished = threading.Event()

    class R:
        def __init__(self, a):
            self.audio_content = a

    class Client:
        def streaming_synthesize(self, requests, timeout):
            list(requests)
            for a in (b"1", b"2", b"3"):
                yield R(a)
            finished.set()

    chunks = GoogleTextToSpeech(client=Client()).synthesize("Hello")
    assert next(chunks) == b"1"   # the speaker is still "playing" the first chunk...
    assert finished.wait(1.0)     # ...but the stream has already been read to the end
    assert list(chunks) == [b"2", b"3"]


# ---- running where there is no sound card (Phase 6: a fresh Pi) --------------------

def _problem(monkeypatch, query):
    import sounddevice as sd

    from chatbox.audio import audio_device_problem

    monkeypatch.setattr(sd, "query_devices", query)
    return audio_device_problem()


def test_no_microphone_is_reported_plainly(monkeypatch):
    devices = [{"max_input_channels": 0, "max_output_channels": 2}]
    problem = _problem(monkeypatch, lambda: devices)
    assert problem is not None and "microphone" in problem


def test_no_sound_card_at_all_is_reported_plainly(monkeypatch):
    import sounddevice as sd

    def boom():
        raise sd.PortAudioError("Error querying device -1")

    problem = _problem(monkeypatch, boom)
    assert problem is not None and "no sound devices" in problem


def test_working_devices_report_no_problem(monkeypatch):
    devices = [{"max_input_channels": 1, "max_output_channels": 2}]
    assert _problem(monkeypatch, lambda: devices) is None


# ---- push-to-talk sources: spacebar or button (PLAN.md D5) -------------------------

class FakeButton:
    def __init__(self, pressed=False):
        self.is_pressed = pressed
        self.closed = False

    def close(self):
        self.closed = True


class FakePress:
    """A press source that answers from a script, for testing AnyPress."""

    def __init__(self, name, commands=()):
        self.name = name
        self.commands = list(commands)
        self.begun = self.flushed = 0
        self.confirmed = True

    def poll_command(self, timeout):
        return self.commands.pop(0) if self.commands else None

    def begin(self):
        self.begun += 1

    def poll_hold(self, timeout):
        pass

    def held(self, now):
        return False

    def flush(self):
        self.flushed += 1


def test_button_reports_talk_only_while_pressed():
    from chatbox.audio.pi import ButtonPress

    button = FakeButton(pressed=False)
    press = ButtonPress(button=button)
    assert press.poll_command(0.0) is None
    assert press.held(time.monotonic()) is False

    button.is_pressed = True
    assert press.poll_command(0.0) == "talk"
    assert press.held(time.monotonic()) is True
    # A physical press needs no repeat heuristic: it counts straight away.
    assert press.confirmed is True


def test_button_flush_waits_for_release_but_gives_up():
    from chatbox.audio.pi import ButtonPress

    press = ButtonPress(button=FakeButton(pressed=True))
    started = time.monotonic()
    press.flush(timeout=0.1)          # still held: returns once the timeout passes
    assert 0.05 < time.monotonic() - started < 1.0


def test_any_press_takes_whichever_source_fires():
    from chatbox.audio.press import AnyPress

    keyboard, button = FakePress("SPACE"), FakePress("the button", ["talk"])
    press = AnyPress([keyboard, button], poll_seconds=0.0)
    assert press.poll_command(None) == "talk"
    assert press.active is button

    # The turn belongs to the source that started it, not the other one.
    press.begin()
    assert button.begun == 1 and keyboard.begun == 0


def test_any_press_flushes_every_source():
    from chatbox.audio.press import AnyPress

    keyboard, button = FakePress("SPACE"), FakePress("the button")
    press = AnyPress([keyboard, button], poll_seconds=0.0)
    press.flush()
    assert keyboard.flushed == 1 and button.flushed == 1


def test_any_press_names_both_sources():
    from chatbox.audio.press import AnyPress

    press = AnyPress([FakePress("SPACE"), FakePress("the button")])
    assert press.name == "SPACE or the button"


def test_keyboard_press_reads_commands():
    from chatbox.audio.laptop import KeyboardPress

    class FakeKeyboard:
        def __init__(self, keys):
            self.keys = list(keys)
            self.flushed = 0

        def read(self, timeout):
            return self.keys.pop(0) if self.keys else None

        def flush(self):
            self.flushed += 1

    press = KeyboardPress(FakeKeyboard([" ", "n", "q", ""]), 0.6, 0.2)
    assert press.poll_command(0.0) == "talk"
    assert press.poll_command(0.0) == "new"
    assert press.poll_command(0.0) == "quit"
    assert press.poll_command(0.0) is None


# ---- the status light (PLAN.md section 1: kids get audio and lights, not text) ------

class FakeLight:
    def __init__(self):
        self.states = []
        self.closed = False

    def show(self, state):
        self.states.append(state)

    def close(self):
        self.closed = True


class FakeLed:
    """Stands in for gpiozero's RGBLED."""

    def __init__(self):
        self.value = None
        self.closed = False

    def close(self):
        self.closed = True


def lit(speaker=None):
    from chatbox.audio.light import LitSpeaker

    light = FakeLight()
    return LitSpeaker(speaker or CollectingSpeaker(), light), light


class CollectingSpeaker:
    def __init__(self):
        self.cues, self.audio = [], b""

    def cue(self, name, **kwargs):
        self.cues.append(name)

    def play(self, audio, sample_rate):
        self.audio += b"".join(audio)


def test_cues_drive_the_light_and_still_reach_the_speaker():
    speaker = CollectingSpeaker()
    lit_speaker, light = lit(speaker)
    for cue in ("listening", "stopped", "cancel", "error"):
        lit_speaker.cue(cue)
    assert light.states == ["listening", "thinking", "idle", "error"]
    assert speaker.cues == ["listening", "stopped", "cancel", "error"]


def test_playing_shows_speaking_then_returns_to_idle():
    lit_speaker, light = lit()
    lit_speaker.play([b"\x00\x00"], 24_000)
    assert light.states == ["speaking", "idle"]


def test_light_returns_to_idle_even_if_playback_fails():
    class Broken:
        def cue(self, name, **kwargs):
            pass

        def play(self, audio, sample_rate):
            raise RuntimeError("speaker gone")

    from chatbox.audio.light import LitSpeaker

    light = FakeLight()
    with pytest.raises(RuntimeError):
        LitSpeaker(Broken(), light).play([b""], 24_000)
    assert light.states == ["speaking", "idle"]


def test_a_whole_spoken_turn_lights_listening_thinking_speaking(make):
    """The light follows a real turn without the voice code knowing it exists.

    The two cues that bracket the recording come from the recorder, not the voice turn:
    "listening" before the mic opens and "stopped" when the button is released. Both go
    through the same speaker, which is what lets one wrapper see every state.
    """
    from chatbox.audio.light import LitSpeaker

    light = FakeLight()
    turn, speaker, _ = make()
    lit_speaker = LitSpeaker(speaker, light)
    turn.speaker = lit_speaker

    def recording():
        lit_speaker.cue("listening")
        yield from speech()
        lit_speaker.cue("stopped")     # released: the pipeline takes over

    turn.run(recording(), RATE)
    assert light.states[0] == "listening"
    assert light.states.index("thinking") < light.states.index("speaking")
    assert light.states[-1] == "idle"      # never left lit


def test_rgb_led_maps_states_to_channels():
    from chatbox.audio.pi import RgbLed

    led = FakeLed()
    light = RgbLed(led=led)
    light.show("listening")
    assert led.value == (0, 1, 0)
    light.show("thinking")
    assert led.value == (1, 1, 0)          # amber: red and green together
    light.show("something new")            # unknown states go dark, never raise
    assert led.value == (0, 0, 0)
    light.close()
    assert led.closed


def test_no_light_is_silent():
    from chatbox.audio.light import NoLight

    light = NoLight()
    light.show("listening")
    light.close()


# ---- per-machine settings (chatbox.local.toml) -------------------------------------

def test_local_config_overrides_only_the_keys_it_names(tmp_path):
    from chatbox.config import load_settings

    (tmp_path / "chatbox.toml").write_text((ROOT / "chatbox.toml").read_text())
    (tmp_path / "chatbox.local.toml").write_text(
        "[voice]\nbutton_gpio = 17\n\n[guardrails.classifier]\ntimeout_seconds = 12\n")

    settings = load_settings(tmp_path / "chatbox.toml")
    assert settings.speech.voice["button_gpio"] == 17
    # Sibling keys in the same sections survive.
    assert settings.speech.voice["sample_rate"] == 16000
    assert settings.guardrails.classifier.timeout_seconds == 12
    assert settings.guardrails.classifier.provider_settings["model"] == "claude-haiku-4-5"
    assert settings.local_config == tmp_path / "chatbox.local.toml"


def test_without_a_local_config_nothing_changes(tmp_path):
    from chatbox.config import load_settings

    (tmp_path / "chatbox.toml").write_text((ROOT / "chatbox.toml").read_text())
    settings = load_settings(tmp_path / "chatbox.toml")
    assert settings.local_config is None
    assert settings.speech.voice.get("button_gpio") is None


def test_local_config_keys_outside_a_section_are_an_error(tmp_path):
    """The failure this prevents: uncommenting `button_gpio` but not `[voice]` above it,
    which is valid TOML that silently does nothing."""
    from chatbox.config import ConfigError, load_settings

    (tmp_path / "chatbox.toml").write_text((ROOT / "chatbox.toml").read_text())
    (tmp_path / "chatbox.local.toml").write_text("button_gpio = 17\nled_gpio = [22, 23, 24]\n")

    with pytest.raises(ConfigError) as e:
        load_settings(tmp_path / "chatbox.toml")
    assert "button_gpio" in str(e.value) and "section" in str(e.value)


def test_unknown_device_name_is_named_with_the_alternatives(monkeypatch):
    """A device name that matches nothing used to surface much later as an empty
    recording, reported as "tap too short"."""
    import sounddevice as sd

    from chatbox.audio import audio_device_problem

    monkeypatch.setattr(sd, "query_devices", _devices_or_lookup)
    problem = audio_device_problem(input_device="No Such Mic")
    assert problem is not None
    assert "No Such Mic" in problem and "Built-in Mic" in problem


def _devices_or_lookup(device=None, kind=None):
    """Stands in for sounddevice.query_devices in both its forms."""
    if device is None:
        return [{"name": "Built-in Mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "Headphones", "max_input_channels": 0, "max_output_channels": 2}]
    raise ValueError(f"no {kind} device matching {device!r}")


def test_known_device_name_is_accepted(monkeypatch):
    import sounddevice as sd

    from chatbox.audio import audio_device_problem

    def query(device=None, kind=None):
        if device is None:
            return _devices_or_lookup()
        return {"name": device}

    monkeypatch.setattr(sd, "query_devices", query)
    assert audio_device_problem(input_device="Built-in Mic") is None


# ---- volume: the speaker scales what it writes -------------------------------------

def test_volume_gain_is_perceptual_not_linear():
    from chatbox.audio.laptop import volume_gain

    assert volume_gain(100) == 1.0
    assert volume_gain(0) == 0.0
    # Halfway on the slider is a quarter of the amplitude (about -12 dB), because a
    # linear slider does almost nothing until its last few percent.
    assert volume_gain(50) == 0.25
    assert volume_gain(150) == 1.0 and volume_gain(-10) == 0.0


def test_scaling_pcm():
    from array import array

    from chatbox.audio.laptop import scale

    pcm = array("h", [10000, -10000, 30000]).tobytes()
    assert array("h", scale(pcm, 0.5)).tolist() == [5000, -5000, 15000]
    assert array("h", scale(pcm, 0.0)).tolist() == [0, 0, 0]
    assert scale(pcm, 1.0) is pcm                      # full volume copies nothing


def test_scaling_never_amplifies():
    """A volume control only attenuates. Past full scale, 16-bit samples wrap, which is
    catastrophic rather than merely loud."""
    from array import array

    from chatbox.audio.laptop import scale

    loud = array("h", [32767, -32768]).tobytes()
    assert array("h", scale(loud, 2.0)).tolist() == [32767, -32768]
    assert array("h", scale(loud, 0.5)).tolist() == [16383, -16384]
