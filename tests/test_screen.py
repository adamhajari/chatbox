"""The picture screen (PLAN.md D28/D29): display and HTTP are both fakes.

The point of nearly every test here is the same: whatever the screen does or fails to
do, the child hears exactly the answer they would have heard without one.
"""

import io
import json
import threading
import time
import urllib.parse

import pytest
from PIL import Image

from chatbox.audio.picture import NoDisplay, PicturedSpeaker, PictureShow, WatchingClassifier
from chatbox.config import load_settings
from chatbox.guardrails import (Classification, classifier_schema, classifier_system_prompt,
                                parse_classification, parse_subject)
from chatbox.policy import Pictures
from chatbox.pictures import (Picture, PictureFinder, _Deadline, find_image, fit, lead_image_url,
                              search_term)
from chatbox.voice import VoiceSettings, VoiceTurn
from chatbox.pipeline import Pipeline
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
        monkeypatch.setattr("chatbox.pictures._get", http.get)
        f = PictureFinder(tmp_path / "pictures", timeout)
        f.http = http
        return f
    return make


class Classifies:
    """A classifier that reports a fixed decision, subject and picture category."""

    def __init__(self, subject="octopus", decision="allow", category="animal"):
        self.subject, self.decision, self.category = subject, decision, category
        self.calls = 0

    def classify(self, question, history, policy):
        self.calls += 1
        topic = None
        if self.decision == "redirect":
            topic = policy.topics.redirect_to_parent[0].id
        return Classification(self.decision, topic, "test", self.subject, self.category)


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


def test_picture_category_is_parsed_from_the_policy_list(policy):
    data = {"decision": "allow", "topic_id": "none", "reason": "fine", "subject": "python",
            "picture_category": "animal"}
    assert parse_classification(data, policy).picture_category == "animal"


@pytest.mark.parametrize("value", ["none", "programming language", "", None, 7])
def test_a_category_outside_the_list_is_none_never_an_error(policy, value):
    data = {"decision": "allow", "topic_id": "none", "reason": "fine", "subject": "python",
            "picture_category": value}
    c = parse_classification(data, policy)
    assert c.decision == "allow" and c.picture_category is None


def test_an_animal_battle_has_an_opponent(policy):
    data = {"decision": "allow", "topic_id": "none", "reason": "fine", "subject": "shark",
            "picture_category": "animal", "opponent": "crocodile"}
    c = parse_classification(data, policy)
    assert (c.subject, c.picture_category, c.opponent) == ("shark", "animal", "crocodile")
    assert "opponent" in classifier_schema(policy)["required"]


@pytest.mark.parametrize("category, opponent", [
    ("space", "Mars"),        # not animals: no battle
    ("none", "crocodile"),
    ("animal", ""),
    ("animal", None),
])
def test_only_an_animal_battle_keeps_an_opponent(policy, category, opponent):
    data = {"decision": "allow", "topic_id": "none", "reason": "fine", "subject": "shark",
            "picture_category": category, "opponent": opponent}
    assert parse_classification(data, policy).opponent is None


def test_the_classifier_is_told_the_parents_categories(policy):
    p = policy.model_copy(update={"pictures": Pictures(categories=["dinosaur", "truck"])})
    prompt = classifier_system_prompt(p)
    assert '"dinosaur", "truck"' in prompt
    assert classifier_schema(p)["properties"]["picture_category"]["enum"] == ["dinosaur", "truck", "none"]


@pytest.mark.parametrize("subject, category, term", [
    ("python", "animal", "python animal"),
    ("  the   Moon ", "space", "the Moon space"),
    ("space shuttle", "space", "space shuttle"),   # already says it
    ("python", None, None),
    ("", "animal", None),
    (None, "animal", None),
])
def test_the_search_term_adds_the_category(subject, category, term):
    assert search_term(subject, category) == term


def test_subject_survives_on_redirect_and_refuse(policy):
    blocked = policy.topics.blocked[0].id
    data = {"decision": "refuse", "topic_id": blocked, "reason": "no", "subject": "knife"}
    c = parse_classification(data, policy)
    assert c.decision == "refuse" and c.topic_id == blocked


def test_bad_output_still_fails_closed_with_a_subject_present(policy):
    """The fail-closed path is unchanged: a subject can't rescue a bad classification."""
    from chatbox.guardrails import GuardrailError

    with pytest.raises(GuardrailError):
        parse_classification({"decision": "sideways", "topic_id": "none", "reason": "?",
                              "subject": "octopus"}, policy)
    with pytest.raises(GuardrailError):
        parse_classification({"decision": "allow", "topic_id": "none", "subject": "x"}, policy)


def test_the_schema_asks_for_a_subject(policy):
    schema = classifier_schema(policy)
    assert schema["properties"]["subject"] == {"type": "string"}
    assert "subject" in schema["required"]
    assert schema["properties"]["picture_category"]["enum"] == [*policy.pictures.categories, "none"]
    assert "picture_category" in schema["required"]
    # Everything the decision depends on is untouched.
    assert schema["properties"]["decision"]["enum"] == ["allow", "redirect", "refuse"]


# ---- fetching the picture (requirement 2) ------------------------------------------

def test_a_subject_becomes_a_sized_picture(finder):
    f = finder()
    picture = f.find("octopus")
    assert picture is not None
    assert picture.image.size == (320, 240) and picture.title == "Octopus"
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
    """The screen is landscape, 320x240 (the panel is 240x320 glass turned 90)."""
    wide = fit(Image.new("RGB", (800, 100), (255, 0, 0)))
    assert wide.size == (320, 240)
    assert wide.getpixel((160, 0)) == (0, 0, 0)        # letterboxed, not stretched
    assert wide.getpixel((160, 120)) == (255, 0, 0)

    tall = fit(Image.new("RGB", (100, 800), (0, 255, 0)))
    assert tall.size == (320, 240)
    assert tall.getpixel((0, 120)) == (0, 0, 0)
    assert tall.getpixel((160, 120)) == (0, 255, 0)


@pytest.mark.parametrize("rotation,size", [(0, (240, 320)), (90, (320, 240)),
                                           (180, (240, 320)), (270, (320, 240))])
def test_the_screens_picture_size_follows_its_rotation(rotation, size):
    from chatbox.audio.pi import Screen, image_size

    assert image_size(rotation) == size
    assert Screen(rotation=rotation, display=object()).size == size


def test_a_picture_of_the_wrong_shape_is_refitted_rather_than_silently_dropped():
    """The driver refuses a mismatched image and show() swallows it, so a rotation the
    finder didn't know about would be an invisible failure."""
    from chatbox.audio.pi import Screen

    class Panel:
        def __init__(self):
            self.images = []

        def image(self, img):
            if img.size != (320, 240):
                raise ValueError("Image must be same dimensions as display")
            self.images.append(img)

        def fill(self, colour):
            pass

    panel = Panel()
    screen = Screen(rotation=90, display=panel)
    screen.show(Image.new("RGB", (240, 320), (1, 2, 3)))   # portrait, the wrong way up
    assert panel.images and panel.images[0].size == (320, 240)


def test_the_api_query_asks_for_the_articles_lead_image(monkeypatch):
    """D29: the article's `pageimage`, not a Commons free-text image search."""
    seen = {}

    def fake_get(url, deadline):
        seen["url"] = url
        return json.dumps({"query": {"pages": [
            {"title": "Moon", "thumbnail": {"source": "https://example.invalid/m.jpg"}}]}}).encode()

    monkeypatch.setattr("chatbox.pictures._get", fake_get)

    assert lead_image_url("the Moon", _Deadline(2)) == ("https://example.invalid/m.jpg", "Moon")
    assert "prop=pageimages" in seen["url"] and "commons" not in seen["url"]


# ---- the bare subject first, then with its category ---------------------------------

class Wikipedia:
    """Stands in for the search: a top article per query, like the real one gives."""

    def __init__(self, articles):
        self.articles, self.searches = articles, []

    def get(self, url, deadline):
        if not url.startswith("https://en.wikipedia.org"):
            return png_bytes()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["gsrsearch"][0]
        self.searches.append(query)
        found = self.articles.get(query)
        if found is None:
            return json.dumps({"query": {}}).encode()
        title, props = found if isinstance(found, tuple) else (found, {})
        page = {"title": title, "thumbnail": {"source": f"https://example.invalid/{title}.jpg"},
                **({"pageprops": props} if props else {})}
        return json.dumps({"query": {"pages": [page]}}).encode()


@pytest.fixture
def wikipedia(monkeypatch):
    def make(articles):
        w = Wikipedia(articles)
        monkeypatch.setattr("chatbox.pictures._get", w.get)
        return w
    return make


def test_a_subject_that_names_an_article_is_looked_up_once(wikipedia):
    w = wikipedia({"Eiffel Tower": "Eiffel Tower",
                   "Eiffel Tower landmark": "Eiffel Tower (Paris, Texas)"})
    assert find_image("Eiffel Tower", "landmark", _Deadline(2))[1] == "Eiffel Tower"
    assert w.searches == ["Eiffel Tower"]


def test_the_moon_names_the_article_moon(wikipedia):
    w = wikipedia({"the Moon": "Moon"})
    assert find_image("the Moon", "space", _Deadline(2))[1] == "Moon"
    assert w.searches == ["the Moon"]


def test_a_guessed_article_is_searched_again_with_the_category(wikipedia):
    w = wikipedia({"python": "Python (programming language)", "python animal": "Burmese python"})
    assert find_image("python", "animal", _Deadline(2))[1] == "Burmese python"
    assert w.searches == ["python", "python animal"]


def test_a_disambiguation_page_is_searched_again_with_the_category(wikipedia):
    w = wikipedia({"mercury": ("Mercury", {"disambiguation": ""}),
                   "mercury space": "Mercury (planet)"})
    assert find_image("mercury", "space", _Deadline(2))[1] == "Mercury (planet)"
    assert w.searches == ["mercury", "mercury space"]


def test_a_subject_that_already_says_its_category_is_searched_once(wikipedia):
    w = wikipedia({"space shuttle": "Space Shuttle program"})
    assert find_image("space shuttle", "space", _Deadline(2))[1] == "Space Shuttle program"
    assert w.searches == ["space shuttle"]


def test_the_finder_caches_by_subject_and_category(wikipedia, tmp_path):
    w = wikipedia({"python": "Python (programming language)", "python animal": "Burmese python"})
    f = PictureFinder(tmp_path / "pictures")
    assert f.find("python", "animal").title == "Burmese python"
    assert f.find("python", "animal").source == "cache"
    assert w.searches == ["python", "python animal"]


# ---- showing and clearing (requirements 3, 4, 5) -----------------------------------

def show_for(display=None, picture=None, finder=None):
    class Fixed:
        def __init__(self):
            self.asked = []

        def find(self, subject, category=None):
            self.asked.append(search_term(subject, category) or subject)
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
        def find(self, subject, category=None):
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
        def find(self, subject, category=None):
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


# ---- an animal battle: two pictures, taking turns -----------------------------------

class ByName:
    """A finder with a picture per subject, recording what it was asked."""

    def __init__(self, *missing, delay=0.0):
        self.missing, self.delay, self.asked = set(missing), delay, []

    def find(self, subject, category=None):
        self.asked.append(subject)
        if self.delay:
            time.sleep(self.delay)
        return None if subject in self.missing else picture_of(subject)


def shown_subjects(display, show):
    return [show_subject(image, show) for image in display.shown]


def show_subject(image, show):
    return next(p.subject for p in show.shown if p.image is image)


def test_a_battle_swaps_between_the_two_animals_while_speaking():
    display = FakeDisplay()
    show = PictureShow(display, ByName(), swap_seconds=0.05)
    show.wanted("shark", "animal", "crocodile")
    assert wait_for(lambda: show._pending.done())
    show.show()
    assert wait_for(lambda: len(display.shown) >= 4)
    show.clear()
    subjects = shown_subjects(display, show)
    assert subjects[:4] == ["shark", "crocodile", "shark", "crocodile"]
    assert [p.subject for p in show.shown] == ["shark", "crocodile"]   # for --verbose


def test_swapping_stops_when_the_answer_ends():
    display = FakeDisplay()
    show = PictureShow(display, ByName(), swap_seconds=0.02)
    show.wanted("shark", "animal", "crocodile")
    assert wait_for(lambda: show._pending.done())
    show.show()
    assert wait_for(lambda: len(display.shown) >= 2)
    show.clear()
    drawn = len(display.shown)
    time.sleep(0.1)
    assert len(display.shown) == drawn and show.last is None


def test_a_battle_with_one_picture_missing_just_shows_the_other():
    display = FakeDisplay()
    show = PictureShow(display, ByName("florb"), swap_seconds=0.02)
    show.wanted("shark", "animal", "florb")
    assert wait_for(lambda: show._pending.done())
    show.show()
    time.sleep(0.1)
    show.clear()
    assert len(display.shown) == 1 and show.previous.subject == "shark"


def test_the_second_animal_arriving_late_joins_the_swap():
    display = FakeDisplay()
    show = PictureShow(display, ByName(delay=0.05), swap_seconds=0.02)
    show.wanted("shark", "animal", "crocodile")
    show.show()                         # nothing ready yet: never waits
    assert display.shown == []
    assert wait_for(lambda: [p.subject for p in show.shown] == ["shark", "crocodile"])
    show.clear()


def test_a_single_subject_is_never_swapped():
    display = FakeDisplay()
    show = PictureShow(display, ByName(), swap_seconds=0.02)
    show.wanted("octopus", "animal")
    assert wait_for(lambda: show._pending.done())
    show.show()
    time.sleep(0.1)
    show.clear()
    assert len(display.shown) == 1


def test_a_finder_that_throws_never_escapes():
    class Broken:
        def find(self, subject, category=None):
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
    assert result == Classification("allow", None, "test", "octopus", "animal")
    assert wait_for(lambda: show.finder.asked == ["octopus animal"])


def test_the_watching_classifier_asks_for_both_animals_in_a_battle(policy):
    class Battle(Classifies):
        def classify(self, question, history, policy):
            return Classification("allow", None, "test", "shark", "animal", "crocodile")

    show = show_for(picture=picture_of())
    WatchingClassifier(Battle(), show).classify("who would win?", [], policy)
    assert wait_for(lambda: show.finder.asked == ["shark animal", "crocodile animal"])


def test_a_subject_with_no_picture_category_shows_nothing(policy):
    show = show_for(picture=picture_of())
    WatchingClassifier(Classifies("birthday", category=None), show).classify("q", [], policy)
    time.sleep(0.05)
    assert show.finder.asked == []


def test_a_redirected_question_shows_nothing(policy):
    show = show_for(picture=picture_of())
    wrapped = WatchingClassifier(Classifies("knife", "redirect"), show)
    result = wrapped.classify("where are the knives?", [], policy)
    assert result.decision == "redirect"
    time.sleep(0.05)
    assert show.finder.asked == []


def test_a_broken_screen_cannot_fail_a_classification(policy):
    class Exploding(PictureShow):
        def wanted(self, subject, category=None):
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

    def find(self, subject, category=None):
        self.asked.append(search_term(subject, category) or subject)
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
    from chatbox.audio.pi import Screen

    class Panel:
        def __init__(self):
            self.images, self.filled = [], 0

        def image(self, img):
            self.images.append(img)

        def fill(self, colour):
            self.filled += 1

    panel = Panel()
    screen = Screen(display=panel)
    image = Image.new("RGB", (320, 240))
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
    from chatbox.audio.pi import Screen

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
    screen.show(Image.new("RGB", (320, 240)))
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
    screen.show(Image.new("RGB", (320, 240)))
    assert backlight.history == []      # nothing was drawn, so nothing is lit


def test_closing_darkens_the_backlight_and_releases_the_pin():
    screen, backlight = screen_with_backlight()
    screen.show(Image.new("RGB", (320, 240)))
    screen.close()
    assert backlight.history[-1] is False and backlight.closed


def test_a_backlight_that_fails_never_breaks_a_turn():
    screen, _ = screen_with_backlight(fail=True)
    screen.show(Image.new("RGB", (320, 240)))   # the panel still has the picture on it
    screen.blank()
    screen.close()


def test_without_a_backlight_pin_nothing_changes():
    from chatbox.audio.pi import Screen

    class Panel:
        def __init__(self):
            self.images, self.filled = [], 0

        def image(self, img):
            self.images.append(img)

        def fill(self, colour):
            self.filled += 1

    panel = Panel()
    screen = Screen(display=panel)
    screen.show(Image.new("RGB", (320, 240)))
    screen.blank()
    assert panel.images and panel.filled == 1


def test_the_backlight_settings_load(tmp_path):
    (tmp_path / "chatbox.toml").write_text((ROOT / "chatbox.toml").read_text())
    (tmp_path / "chatbox.local.toml").write_text(
        "[screen]\nenabled = true\nbacklight_gpio = 12\nbacklight_active_high = false\n")
    screen = load_settings(tmp_path / "chatbox.toml").screen
    assert screen.backlight_gpio == 12 and screen.backlight_active_high is False


def test_the_backlight_is_off_unless_a_machine_names_a_pin(tmp_path):
    (tmp_path / "chatbox.toml").write_text((ROOT / "chatbox.toml").read_text())
    (tmp_path / "chatbox.local.toml").write_text("[screen]\nenabled = true\n")
    assert load_settings(tmp_path / "chatbox.toml").screen.backlight_gpio is None


def test_the_screen_adapter_explains_itself_when_the_library_is_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_blinka(name, *args, **kwargs):
        if name in {"board", "busio", "digitalio", "adafruit_rgb_display"}:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_blinka)
    from chatbox.audio.pi import Screen

    with pytest.raises(RuntimeError) as e:
        Screen()
    assert "adafruit" in str(e.value) and "SPI" in str(e.value)


# ---- settings (requirement 6) ------------------------------------------------------

def test_the_tracked_config_ships_the_screen_off():
    """chatbox.toml documents the options; only the Pi's local file turns it on."""
    assert load_settings(ROOT / "chatbox.toml").screen is None


def test_a_local_file_turns_the_screen_on(tmp_path):
    (tmp_path / "chatbox.toml").write_text((ROOT / "chatbox.toml").read_text())
    (tmp_path / "chatbox.local.toml").write_text("[screen]\nenabled = true\ntimeout_seconds = 2\n")
    screen = load_settings(tmp_path / "chatbox.toml").screen
    assert screen is not None
    assert screen.timeout_seconds == 2
    assert (screen.dc_gpio, screen.reset_gpio, screen.cs) == (25, 27, 0)  # from chatbox.toml
    assert screen.rotation == 90   # landscape
    assert screen.cache_dir is None
    assert screen.swap_seconds == 3


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
    assert picture.title == "Octopus" and picture.image.size == (320, 240)


@pytest.mark.live
def test_the_user_agent_is_one_wikimedia_accepts():
    from chatbox.pictures import USER_AGENT

    assert "Chatbox" in USER_AGENT
    # A bare scheme-and-host with nothing behind it reads as a placeholder and is
    # refused; it must be a page that exists.
    assert "https://github.com/;" not in USER_AGENT


def test_verbose_can_report_the_picture_after_the_screen_has_been_cleared():
    """A spoken turn blanks the screen before --verbose prints its steps, so reporting
    from `last` printed nothing, ever."""
    show = show_for(picture=picture_of())
    show.wanted("octopus")
    assert wait_for(lambda: show._pending.done())
    show.show()
    show.clear()
    assert show.last is None                       # the screen really is blank
    assert show.previous.subject == "octopus"      # but we can still say what was on it


def test_a_new_question_forgets_the_last_picture():
    show = show_for(picture=picture_of())
    show.wanted("octopus")
    assert wait_for(lambda: show._pending.done())
    show.show()
    show.clear()
    show.wanted(None)          # a question with no subject
    assert show.previous is None


def test_a_lookup_in_flight_is_reported_as_still_looking():
    """`chat` prints its answer the instant the classifier returns, so the lookup has
    usually barely started -- "nothing" would be the wrong thing to say."""
    release = threading.Event()

    class Slow:
        def find(self, subject, category=None):
            release.wait(2.0)
            return picture_of()

    show = PictureShow(FakeDisplay(), Slow())
    assert show.looking is False
    show.wanted("octopus")
    assert show.looking is True and show.previous is None
    release.set()
    assert wait_for(lambda: not show.looking)


def test_no_subject_is_not_still_looking():
    show = show_for(picture=picture_of())
    show.wanted(None)
    assert show.looking is False and show.previous is None
