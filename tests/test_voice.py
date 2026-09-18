"""Voice turn tests: speech services, microphone and speaker are all fakes."""

import time
from array import array

import pytest

from talkbox.audio.cues import cue_pcm
from talkbox.audio.laptop import HoldDetector
from talkbox.config import load_settings
from talkbox.guardrails import Classification, OutputVerdict
from talkbox.pipeline import Pipeline
from talkbox.speech import SpeechError, Transcript, make_stt, make_tts
from talkbox.speech.google import GoogleSpeechToText, GoogleTextToSpeech
from talkbox.voice import VoiceSettings, VoiceTurn
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
    sp = load_settings(ROOT / "talkbox.toml").speech
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
