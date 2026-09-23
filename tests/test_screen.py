"""The picture screen (PLAN.md D28/D29): display and HTTP are both fakes.

The point of nearly every test here is the same: whatever the screen does or fails to
do, the child hears exactly the answer they would have heard without one.
"""

import io
import json
import threading
import time

import pytest
from PIL import Image

from talkbox.audio.picture import NoDisplay, PicturedSpeaker, PictureShow, WatchingClassifier
from talkbox.config import load_settings
from talkbox.guardrails import Classification, classifier_schema, parse_classification, parse_subject
from talkbox.pictures import Picture, PictureFinder, fit, lead_image_url
from talkbox.voice import VoiceSettings, VoiceTurn
from talkbox.pipeline import Pipeline
from tests.conftest import ROOT, WED_NOON, FakeProvider, PassAll
from tests.test_voice import Events, FakeSpeaker, FakeSTT, FakeTTS, RATE, speech


def png_bytes(size=(400, 300), colour=(10, 120, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


# ---- fakes -------------------------------------------------------------------------

class FakeDisplay:
    """Stands in for the ILI9341 panel."""

    def __init__(self, fail=False):
        self.shown, self.blanks, self.closed, self.fail = [], 0, False, fail

    def show(self, image):
        if self.fail:
            raise RuntimeError("SPI write failed")
        self.shown.append(image)

    def blank(self):
        self.blanks += 1

    def close(self):
        self.closed = True


class FakeHttp:
    """Stands in for the network: one canned API response and one image."""

    def __init__(self, article="Octopus", image=True, error=None, delay=0.0):
        self.article, self.image, self.error, self.delay = article, image, error, delay
        self.calls = []

    def api(self, url, deadline):
        self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        deadline.check()
        if self.error:
            raise self.error
        pages = []
        if self.article:
            page = {"title": self.article}
            if self.image:
                page["thumbnail"] = {"source": "https://example.invalid/octopus.jpg"}
            pages = [page]
        return json.dumps({"query": {"pages": pages}}).encode()

    def get(self, url, deadline):
        if url.startswith("https://en.wikipedia.org"):
            return self.api(url, deadline)
        self.calls.append(url)
        deadline.check()
        return png_bytes()


@pytest.fixture
def finder(tmp_path, monkeypatch):
    def make(http=None, timeout=3.0):
        http = http or FakeHttp()
        monkeypatch.setattr("talkbox.pictures._get", http.get)
        f = PictureFinder(tmp_path / "pictures", timeout)
        f.http = http
        return f
    return make


class Classifies:
    """A classifier that reports a fixed decision and subject."""

    def __init__(self, subject="octopus", decision="allow"):
        self.subject, self.decision = subject, decision
        self.calls = 0

    def classify(self, question, history, policy):
        self.calls += 1
        topic = None
        if self.decision == "redirect":
            topic = policy.topics.redirect_to_parent[0].id
        return Classification(self.decision, topic, "test", self.subject)


# ---- the classifier reports a subject (requirement 1) ------------------------------

def test_subject_is_parsed_but_never_decides_anything(policy):
    data = {"decision": "allow", "topic_id": "none", "reason": "fine", "subject": "octopus"}
    c = parse_classification(data, policy)
    assert c.decision == "allow" and c.topic_id is None and c.subject == "octopus"


@pytest.mark.parametrize("value", ["", "  ", "none", "N/A", None, 7, ["octopus"], "x" * 61])
def test_an_unusable_subject_is_none_never_an_error(policy, value):
    data = {"decision": "allow", "topic_id": "none", "reason": "fine", "subject": value}
    assert parse_classification(data, policy).subject is None
    assert parse_subject(value) is None


def test_a_missing_subject_still_classifies(policy):
    """Old-shaped output (no subject at all) must keep working exactly as before."""
    data = {"decision": "allow", "topic_id": "none", "reason": "fine"}
    c = parse_classification(data, policy)
    assert c.decision == "allow" and c.subject is None


def test_subject_survives_on_redirect_and_refuse(policy):
    blocked = policy.topics.blocked[0].id
    data = {"decision": "refuse", "topic_id": blocked, "reason": "no", "subject": "knife"}
    c = parse_classification(data, policy)
    assert c.decision == "refuse" and c.topic_id == blocked


def test_bad_output_still_fails_closed_with_a_subject_present(policy):
    """The fail-closed path is unchanged: a subject can't rescue a bad classification."""
    from talkbox.guardrails import GuardrailError

    with pytest.raises(GuardrailError):
        parse_classification({"decision": "sideways", "topic_id": "none", "reason": "?",
                              "subject": "octopus"}, policy)
    with pytest.raises(GuardrailError):
        parse_classification({"decision": "allow", "topic_id": "none", "subject": "x"}, policy)


def test_the_schema_asks_for_a_subject(policy):
    schema = classifier_schema(policy)
    assert schema["properties"]["subject"] == {"type": "string"}
    assert "subject" in schema["required"]
    # Everything the decision depends on is untouched.
    assert schema["properties"]["decision"]["enum"] == ["allow", "redirect", "refuse"]


# ---- fetching the picture (requirement 2) ------------------------------------------

def test_a_subject_becomes_a_sized_picture(finder):
    f = finder()
    picture = f.find("octopus")
    assert picture is not None
    assert picture.image.size == (240, 320) and picture.title == "Octopus"
    assert "gsrsearch=octopus" in f.http.calls[0] and "pageimages" in f.http.calls[0]


def test_the_same_subject_is_not_fetched_twice(finder):
    f = finder()
    assert f.find("octopus") is not None
    calls = len(f.http.calls)
    again = f.find("octopus")
    assert again is not None and again.source == "cache"
    assert len(f.http.calls) == calls  # straight off the disk


def test_no_article_found_is_no_picture_and_is_remembered(finder):
    f = finder(FakeHttp(article=None))
    assert f.find("florb") is None
    f.find("florb")
    assert len(f.http.calls) == 1  # the miss is cached: no retry storm


def test_an_article_with_no_lead_image_is_no_picture(finder):
    f = finder(FakeHttp(image=False))
    assert f.find("sleep") is None


def test_a_fetch_error_is_no_picture_and_is_not_cached_as_a_miss(finder):
    f = finder(FakeHttp(error=OSError("network is unreachable")))
    assert f.find("octopus") is None
    assert f.find("octopus") is None
    # Retried next time: an outage says nothing about whether a picture exists.
    assert len(f.http.calls) == 2


def test_a_slow_fetch_gives_up_at_the_timeout(finder):
    f = finder(FakeHttp(delay=0.3), timeout=0.05)
    started = time.monotonic()
    assert f.find("octopus") is None
    assert time.monotonic() - started < 1.0


def test_an_empty_subject_never_touches_the_network(finder):
    f = finder()
    assert f.find("") is None and f.find(None) is None
    assert f.http.calls == []


def test_a_corrupt_cache_file_just_refetches(finder):
    f = finder()
    assert f.find("octopus") is not None
    image_path, _ = f._paths("octopus")
    image_path.write_bytes(b"not a png")
    assert f.find("octopus") is not None
    assert len(f.http.calls) == 4  # two requests per real fetch


def test_fit_keeps_the_shape_and_fills_the_screen():
    wide = fit(Image.new("RGB", (800, 100), (255, 0, 0)))
    assert wide.size == (240, 320)
    assert wide.getpixel((120, 0)) == (0, 0, 0)        # letterboxed, not stretched
    assert wide.getpixel((120, 160)) == (255, 0, 0)


def test_the_api_query_asks_for_the_articles_lead_image(monkeypatch):
    """D29: the article's `pageimage`, not a Commons free-text image search."""
    seen = {}

    def fake_get(url, deadline):
        seen["url"] = url
        return json.dumps({"query": {"pages": [
            {"title": "Moon", "thumbnail": {"source": "https://example.invalid/m.jpg"}}]}}).encode()

    monkeypatch.setattr("talkbox.pictures._get", fake_get)
    from talkbox.pictures import _Deadline

    assert lead_image_url("the Moon", _Deadline(2)) == ("https://example.invalid/m.jpg", "Moon")
    assert "prop=pageimages" in seen["url"] and "commons" not in seen["url"]


# ---- showing and clearing (requirements 3, 4, 5) -----------------------------------

def show_for(display=None, picture=None, finder=None):
    class Fixed:
        def __init__(self):
            self.asked = []

        def find(self, subject):
            self.asked.append(subject)
            return picture

    return PictureShow(display or FakeDisplay(), finder or Fixed())


def picture_of(subject="octopus"):
    return Picture(Image.new("RGB", (240, 320), (1, 2, 3)), subject, subject.title(), "test")


def wait_for(condition, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_the_picture_goes_up_with_the_answer_and_comes_down_after():
    display = FakeDisplay()
    show = show_for(display, picture_of())
    show.wanted("octopus")
    assert wait_for(lambda: show._pending.done())
    show.show()
    assert len(display.shown) == 1 and show.last.subject == "octopus"
    show.clear()
    assert display.blanks == 1 and show.last is None


def test_no_subject_means_no_lookup_and_a_blank_screen():
    display = FakeDisplay()
    show = show_for(display, picture_of())
    show.wanted(None)
    show.show()
    assert display.shown == []


def test_a_picture_that_is_not_ready_does_not_delay_the_answer():
    """show() must return at once even with a lookup still running (requirement 3)."""
    started = threading.Event()

    class Slow:
        def find(self, subject):
            started.set()
            time.sleep(0.5)
            return picture_of()

    display = FakeDisplay()
    show = PictureShow(display, Slow())
    show.wanted("octopus")
    assert started.wait(1.0)
    at = time.monotonic()
    show.show()
    assert time.monotonic() - at < 0.1 and display.shown == []
    # It arrives late instead, while the answer is still being spoken.
    assert wait_for(lambda: display.shown)


def test_a_late_picture_for_a_finished_turn_is_dropped():
    release = threading.Event()

    class Slow:
        def find(self, subject):
            release.wait(2.0)
            return picture_of()

    display = FakeDisplay()
    show = PictureShow(display, Slow())
    show.wanted("octopus")
    show.show()
    show.clear()          # the turn ended before the picture arrived
    release.set()
    time.sleep(0.2)
    assert display.shown == []   # nothing appears on a screen that should be blank


def test_a_display_that_throws_never_escapes():
    show = show_for(FakeDisplay(fail=True), picture_of())
    show.wanted("octopus")
    assert wait_for(lambda: show._pending.done())
    show.show()       # the SPI write raises inside; nothing comes out
    show.clear()
    assert show.last is None


def test_a_finder_that_throws_never_escapes():
    class Broken:
        def find(self, subject):
            raise RuntimeError("boom")

    show = PictureShow(FakeDisplay(), Broken())
    show.wanted("octopus")
    assert wait_for(lambda: show._pending.done())
    show.show()


def test_no_display_is_silent():
    display = NoDisplay()
    display.show(object())
    display.blank()
    display.close()


# ---- the two wrappers, as light.py does it -----------------------------------------

def test_the_watching_classifier_starts_the_lookup_and_classifies_unchanged(policy):
    show = show_for(picture=picture_of())
    inner = Classifies("octopus")
    wrapped = WatchingClassifier(inner, show)
    result = wrapped.classify("what is an octopus?", [], policy)
    assert result == Classification("allow", None, "test", "octopus")
    assert wait_for(lambda: show.finder.asked == ["octopus"])


def test_a_redirected_question_shows_nothing(policy):
    show = show_for(picture=picture_of())
    wrapped = WatchingClassifier(Classifies("knife", "redirect"), show)
    result = wrapped.classify("where are the knives?", [], policy)
    assert result.decision == "redirect"
    time.sleep(0.05)
    assert show.finder.asked == []


def test_a_broken_screen_cannot_fail_a_classification(policy):
    class Exploding(PictureShow):
        def wanted(self, subject):
            raise RuntimeError("screen is on fire")

    show = Exploding(FakeDisplay(), None)
    result = WatchingClassifier(Classifies(), show).classify("q", [], policy)
    assert result.decision == "allow"


def test_the_speaker_wrapper_shows_on_play_and_blanks_after():
    display = FakeDisplay()
    show = show_for(display, picture_of())
    show.wanted("octopus")
    assert wait_for(lambda: show._pending.done())

    class Inner:
        def __init__(self):
            self.cues, self.played = [], []

        def cue(self, name, **kwargs):
            self.cues.append(name)

        def play(self, audio, sample_rate):
            self.played += list(audio)

    inner = Inner()
    speaker = PicturedSpeaker(inner, show)
    speaker.play([b"\x00\x00"], 24_000)
    assert inner.played == [b"\x00\x00"]
    assert len(display.shown) == 1 and display.blanks == 1


def test_the_screen_blanks_even_when_playback_fails():
    class Broken:
        def cue(self, name, **kwargs):
            pass

        def play(self, audio, sample_rate):
            raise RuntimeError("speaker gone")

    display = FakeDisplay()
    with pytest.raises(RuntimeError):
        PicturedSpeaker(Broken(), show_for(display, picture_of())).play([b""], 24_000)
    assert display.blanks == 1


def test_an_error_or_cancel_cue_blanks_the_screen():
    display = FakeDisplay()
    show = show_for(display, picture_of())
    inner = FakeSpeaker(Events())
    speaker = PicturedSpeaker(inner, show)
    for cue in ("listening", "cancel", "error"):
        speaker.cue(cue)
    assert display.blanks == 3 and inner.cues == ["listening", "cancel", "error"]


# ---- a whole spoken turn, screen and all (requirement 8) ---------------------------

def whole_turn(policy, counter, classifier, finder, display, tts_error=None):
    """A real VoiceTurn with the screen wrapped around it the way the CLI does."""
    events = Events()
    show = PictureShow(display, finder)
    pipeline = Pipeline(policy, FakeProvider(), counter, classifier=classifier,
                        output_checker=PassAll(), clock=lambda: WED_NOON)
    pipeline.classifier = WatchingClassifier(pipeline.classifier, show)
    speaker = PicturedSpeaker(FakeSpeaker(events), show)
    tts = FakeTTS(events, error=tts_error)
    turn = VoiceTurn(pipeline, FakeSTT(), tts, speaker, VoiceSettings())
    return turn, tts, show


STEPS = [("stt", "ok"), ("limits", "allow"), ("classify", "allow"), ("canned", "skip"),
         ("generate", "ok"), ("output_check", "pass"), ("pipeline", "model"),
         ("tts", "ok"), ("speech_start", "ok")]


class FixedFinder:
    def __init__(self, picture=None, error=None, delay=0.0):
        self.picture, self.error, self.delay = picture, error, delay
        self.asked = []

    def find(self, subject):
        self.asked.append(subject)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.picture


@pytest.mark.parametrize("finder,classifier", [
    (FixedFinder(picture_of()), Classifies("octopus")),          # everything works
    (FixedFinder(None), Classifies("florb")),                    # no article found
    (FixedFinder(error=OSError("unreachable")), Classifies("octopus")),   # fetch error
    (FixedFinder(picture_of(), delay=0.4), Classifies("octopus")),        # too slow
    (FixedFinder(picture_of()), Classifies(None)),               # no subject
])
def test_the_spoken_answer_is_the_same_whatever_the_screen_does(policy, counter,
                                                                finder, classifier):
    display = FakeDisplay()
    turn, tts, show = whole_turn(policy, counter, classifier, finder, display)
    result = turn.run(speech(1.0), RATE)
    assert result.spoken == "The sky is blue because of sunlight."
    assert tts.texts == [result.spoken]
    assert [(s["step"], s["decision"]) for s in result.steps] == STEPS
    assert display.blanks >= 1        # the screen never stays lit after the turn
    assert show.last is None


def test_a_broken_display_does_not_break_a_turn(policy, counter):
    display = FakeDisplay(fail=True)
    turn, tts, _ = whole_turn(policy, counter, Classifies("octopus"),
                              FixedFinder(picture_of()), display)
    result = turn.run(speech(1.0), RATE)
    assert result.spoken == tts.texts[0] == "The sky is blue because of sunlight."


def test_the_pi_screen_adapter_swallows_display_failures():
    from talkbox.audio.pi import Screen

    class Panel:
        def __init__(self):
            self.images, self.filled = [], 0

        def image(self, img):
            self.images.append(img)

        def fill(self, colour):
            self.filled += 1

    panel = Panel()
    screen = Screen(display=panel)
    image = Image.new("RGB", (240, 320))
    screen.show(image)
    assert panel.images == [image]
    screen.blank()
    screen.close()
    assert panel.filled == 2

    class Dead:
        def image(self, img):
            raise OSError("SPI down")

        def fill(self, colour):
            raise OSError("SPI down")

    dead = Screen(display=Dead())
    dead.show(image)   # unknown or failed states go blank rather than raising
    dead.blank()
    dead.close()


class FakeBacklight:
    """Stands in for gpiozero's DigitalOutputDevice on the backlight pin."""

    def __init__(self, fail=False):
        self.closed, self.fail = False, fail
        self.history = []          # every write, in order
        self._value = False

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, on):
        if self.fail:
            raise OSError("GPIO gone")
        self._value = on
        self.history.append(on)

    def close(self):
        self.closed = True


def screen_with_backlight(fail=False, panel=None):
    from talkbox.audio.pi import Screen

    class Panel:
        def __init__(self):
            self.images, self.filled = [], 0

        def image(self, img):
            self.images.append(img)

        def fill(self, colour):
            self.filled += 1

    backlight = FakeBacklight(fail)
    return Screen(display=panel or Panel(), backlight=backlight), backlight


def test_the_backlight_lights_with_the_picture_and_goes_out_with_it():
    screen, backlight = screen_with_backlight()
    assert backlight.history == []          # starts off: dark until there is something
    screen.show(Image.new("RGB", (240, 320)))
    assert backlight.history == [True]
    screen.blank()
    # Off before the panel is filled, so the picture never flashes black on the way out.
    assert backlight.history == [True, False]


def test_a_picture_that_never_reached_the_panel_does_not_light_the_backlight():
    class Dead:
        def image(self, img):
            raise OSError("SPI down")

        def fill(self, colour):
            pass

    screen, backlight = screen_with_backlight(panel=Dead())
    screen.show(Image.new("RGB", (240, 320)))
    assert backlight.history == []      # nothing was drawn, so nothing is lit


def test_closing_darkens_the_backlight_and_releases_the_pin():
    screen, backlight = screen_with_backlight()
    screen.show(Image.new("RGB", (240, 320)))
    screen.close()
    assert backlight.history[-1] is False and backlight.closed


def test_a_backlight_that_fails_never_breaks_a_turn():
    screen, _ = screen_with_backlight(fail=True)
    screen.show(Image.new("RGB", (240, 320)))   # the panel still has the picture on it
    screen.blank()
    screen.close()


def test_without_a_backlight_pin_nothing_changes():
    from talkbox.audio.pi import Screen

    class Panel:
        def __init__(self):
            self.images, self.filled = [], 0

        def image(self, img):
            self.images.append(img)

        def fill(self, colour):
            self.filled += 1

    panel = Panel()
    screen = Screen(display=panel)
    screen.show(Image.new("RGB", (240, 320)))
    screen.blank()
    assert panel.images and panel.filled == 1


def test_the_backlight_settings_load(tmp_path):
    (tmp_path / "talkbox.toml").write_text((ROOT / "talkbox.toml").read_text())
    (tmp_path / "talkbox.local.toml").write_text(
        "[screen]\nenabled = true\nbacklight_gpio = 12\nbacklight_active_high = false\n")
    screen = load_settings(tmp_path / "talkbox.toml").screen
    assert screen.backlight_gpio == 12 and screen.backlight_active_high is False


def test_the_backlight_is_off_unless_a_machine_names_a_pin(tmp_path):
    (tmp_path / "talkbox.toml").write_text((ROOT / "talkbox.toml").read_text())
    (tmp_path / "talkbox.local.toml").write_text("[screen]\nenabled = true\n")
    assert load_settings(tmp_path / "talkbox.toml").screen.backlight_gpio is None


def test_the_screen_adapter_explains_itself_when_the_library_is_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_blinka(name, *args, **kwargs):
        if name in {"board", "busio", "digitalio", "adafruit_rgb_display"}:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_blinka)
    from talkbox.audio.pi import Screen

    with pytest.raises(RuntimeError) as e:
        Screen()
    assert "adafruit" in str(e.value) and "SPI" in str(e.value)


# ---- settings (requirement 6) ------------------------------------------------------

def test_the_tracked_config_ships_the_screen_off():
    """talkbox.toml documents the options; only the Pi's local file turns it on."""
    assert load_settings(ROOT / "talkbox.toml").screen is None


def test_a_local_file_turns_the_screen_on(tmp_path):
    (tmp_path / "talkbox.toml").write_text((ROOT / "talkbox.toml").read_text())
    (tmp_path / "talkbox.local.toml").write_text("[screen]\nenabled = true\ntimeout_seconds = 2\n")
    screen = load_settings(tmp_path / "talkbox.toml").screen
    assert screen is not None
    assert screen.timeout_seconds == 2
    assert (screen.dc_gpio, screen.reset_gpio, screen.cs) == (25, 27, 0)  # from talkbox.toml
    assert screen.cache_dir is None


# ---- the real Wikipedia API (pytest -m live) ---------------------------------------

@pytest.mark.live
def test_a_real_subject_really_fetches_a_picture(tmp_path):
    """The fakes above can't catch a request Wikimedia rejects.

    This is how the User-Agent was found to matter: a placeholder contact URL in it is
    answered with HTTP 429 from the very first request, and the silent-failure design
    everywhere else turns that into "no picture, ever" with nothing in sight.
    """
    picture = PictureFinder(tmp_path, 8.0).find("octopus")
    assert picture is not None, "no picture for 'octopus': check USER_AGENT and the network"
    assert picture.title == "Octopus" and picture.image.size == (240, 320)


@pytest.mark.live
def test_the_user_agent_is_one_wikimedia_accepts():
    from talkbox.pictures import USER_AGENT

    assert "Talkbox" in USER_AGENT
    # A bare scheme-and-host with nothing behind it reads as a placeholder and is
    # refused; it must be a page that exists.
    assert "https://github.com/;" not in USER_AGENT
