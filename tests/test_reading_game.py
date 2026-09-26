"""The reading game: words by reading level, each verdict's effect, the daily count, and
the word staying on the screen, with the models mocked."""

import random

import pytest
from pydantic import ValidationError

from chatbox.audio.picture import PictureShow, WatchingClassifier, word_image
from chatbox.guardrails import Classification
from chatbox.pipeline import Pipeline
from chatbox.policy import Policy
from chatbox.providers.base import ProviderError, StructuredReply, Usage
from chatbox.reading_game import (
    GAME_ID,
    GRADES,
    LAST_WORD,
    NEXT_WORD,
    START_REPLY,
    STOP_REPLY,
    WORDS,
    WORDS_PER_GAME,
    ReadingGame,
    reading_system_prompt,
    tell_word,
    verdict_schema,
)
from tests.conftest import WED_NOON, PassAll
from tests.test_screen import FakeDisplay

START = "let's play the reading game"
STOP = "I'm done playing"
TODAY = "2026-09-16"


class Modes:
    MODES = {START: "start_reading_game", "play again": "start_reading_game",
             STOP: "stop_game"}

    def classify(self, question, history, policy):
        return Classification("allow", detail="test", mode_change=self.MODES.get(question))


class Judge:
    """A provider whose verdict is scripted: the child's words map to a result."""

    name, model = "fake", "fake-model"

    def __init__(self, results=None, data=None):
        self.results, self.data, self.calls = results or {}, data, []

    def generate(self, system, messages):
        raise AssertionError("the reading game asks for a verdict, not plain text")

    def generate_structured(self, system, messages, schema):
        self.calls.append((system, messages, schema))
        if self.data is not None:
            return StructuredReply(self.data, Usage(self.model, 300, 30))
        result = self.results.get(messages[-1].text, "correct")
        return StructuredReply({"result": result, "reply": f"({result})"},
                               Usage(self.model, 300, 30))


@pytest.fixture
def make(counter):
    def _make(policy, provider=None, checker=None):
        provider = provider or Judge()
        p = Pipeline(policy, provider, counter, clock=lambda: WED_NOON, classifier=Modes(),
                     output_checker=checker or PassAll(), rng=random.Random(3))
        p.words_shown = []
        p.show_word = p.words_shown.append
        return p, provider
    return _make


def steps(a):
    return [(s["step"], s["decision"]) for s in a.steps]


def at_grade(policy_data, grade, **reading):
    policy_data["games"] = {"reading": {"grade": grade, **reading}}
    return Policy.model_validate(policy_data)


def test_asking_to_play_shows_the_first_word(policy, make):
    p, provider = make(policy)
    a = p.ask(START)
    assert a.text == START_REPLY and a.answered_by == "canned"
    assert provider.calls == []  # starting needs no model call
    assert isinstance(p.game, ReadingGame) and len(p.game.words) == WORDS_PER_GAME
    assert p.words_shown == [p.game.words[0]]
    assert ("game", "start") in steps(a)


@pytest.mark.parametrize("grade", GRADES)
def test_words_come_from_the_parents_reading_level(policy_data, make, grade):
    p, _ = make(at_grade(policy_data, grade))
    p.ask(START)
    assert set(p.game.words) <= set(WORDS[grade])
    assert len(set(p.game.words)) == WORDS_PER_GAME


def test_the_model_is_told_the_word_and_asked_for_a_verdict(policy, make):
    p, provider = make(policy)
    p.ask(START)
    word = p.game.word
    p.ask(word)
    system, messages, schema = provider.calls[0]
    assert f'The word on the screen now is: "{word}".' in system
    assert "kindergarten" in system
    assert schema == verdict_schema()
    assert messages[-1].text == word


def test_reading_it_right_moves_to_the_next_word(policy, make):
    p, _ = make(policy)
    p.ask(START)
    first = p.game.words[0]
    a = p.ask(first)
    assert a.text == f"(correct) {NEXT_WORD}" and a.answered_by == "model"
    assert p.game.index == 1 and p.words_shown == p.game.words[:2]
    assert ("game", "next") in steps(a)


def test_a_wrong_try_stays_on_the_word_and_is_counted(policy, make):
    p, provider = make(policy, Judge({"dog": "try_again"}))
    p.ask(START)
    a = p.ask("dog")
    assert a.text == "(try_again)" and p.game.index == 0 and p.game.wrong_tries == 1


def test_giving_up_tells_the_word_and_moves_on(policy, make):
    p, _ = make(policy, Judge({"I don't know": "gave_up"}))
    p.ask(START)
    word = p.game.word
    a = p.ask("I don't know")
    assert a.text == f"{tell_word(word)} {NEXT_WORD}"
    assert p.game.index == 1 and p.game.wrong_tries == 0


def test_chat_during_the_game_changes_nothing(policy, make):
    p, _ = make(policy, Judge({"I like pizza": "other"}))
    p.ask(START)
    p.ask("I like pizza")
    assert p.game.index == 0 and p.game.wrong_tries == 0


def test_a_new_word_starts_a_fresh_conversation(policy, make):
    p, provider = make(policy, Judge({"dog": "try_again"}))
    p.ask(START)
    first = p.game.word
    p.ask("dog")
    p.ask(first)
    p.ask("dog")
    # The old word's wrong tries are gone; only the answer that finished it is kept.
    assert [m.text for m in provider.calls[-1][1]] == [
        first, f"(correct) {NEXT_WORD}", "dog"]


def test_the_last_word_ends_the_game_and_counts_it(policy, make):
    p, _ = make(policy)
    p.ask(START)
    for _ in range(WORDS_PER_GAME - 1):
        p.ask(p.game.word)
    a = p.ask(p.game.word)
    assert a.text == f"(correct) {LAST_WORD}"
    assert p.game is None and ("game", "over") in steps(a)
    assert p.counter.games(TODAY, GAME_ID) == 1
    assert p.words_shown[-1] is None  # the screen goes back to normal
    assert p.counter.get(TODAY) == 0  # nothing in the game counted as a question


def test_stopping_partway_doesnt_count(policy, make):
    p, _ = make(policy)
    p.ask(START)
    p.ask(p.game.word)
    a = p.ask(STOP)
    assert a.text == STOP_REPLY and p.game is None and p.words_shown[-1] is None
    assert p.counter.games(TODAY, GAME_ID) == 0


def test_play_again_avoids_the_last_games_words(policy_data, make):
    p, _ = make(at_grade(policy_data, "4"))
    p.ask(START)
    first = list(p.game.words)
    p.ask(STOP)
    p.ask("play again")
    assert not set(p.game.words) & set(first)


def test_a_blocked_answer_doesnt_move_the_game_on(policy, make):
    from tests.test_pipeline_and_log import FailAll
    p, _ = make(policy, checker=FailAll())
    p.ask(START)
    a = p.ask(p.game.word)
    assert a.text == policy.canned_replies.blocked_topic
    assert p.game.index == 0  # the child never heard "yes", so it's still this word


@pytest.mark.parametrize("data", [
    {"result": "maybe", "reply": "hm"},
    {"result": "correct"},
    {"result": "correct", "reply": "  "},
])
def test_an_unusable_verdict_fails_closed(policy, make, data):
    p, _ = make(policy, Judge(data=data))
    p.ask(START)
    a = p.ask("cat")
    assert a.text == policy.canned_replies.something_went_wrong
    assert p.game.index == 0


def test_a_provider_error_fails_closed(policy, make):
    class Broken(Judge):
        def generate_structured(self, *a):
            raise ProviderError("down")

    p, _ = make(policy, Broken())
    p.ask(START)
    assert p.ask("cat").text == policy.canned_replies.something_went_wrong


def test_daily_limit_and_turning_it_off(policy_data, make):
    policy = at_grade(policy_data, "K", daily_games=1)
    p, _ = make(policy)
    p.ask(START)
    for _ in range(WORDS_PER_GAME):
        p.ask(p.game.word)
    a = p.ask("play again")
    assert a.text == policy.games.reading.unavailable_reply and p.game is None

    off = at_grade(policy_data, "K", enabled=False)
    p2, _ = make(off)
    assert p2.ask(START).text == off.games.reading.unavailable_reply


def test_reading_level_is_validated():
    from chatbox.policy import Reading
    assert Reading(grade=2).grade == "2"
    assert Reading(grade="k").grade == "K"
    with pytest.raises(ValidationError):
        Reading(grade="6")


def test_word_lists():
    for grade in GRADES:
        words = WORDS[grade]
        assert len(words) >= 3 * WORDS_PER_GAME, grade
        assert len(words) == len(set(words)), grade
        assert all(w == w.lower() and w.isalpha() and len(w) > 1 for w in words), grade


def test_prompt_never_reads_the_word_for_the_child(policy):
    s = reading_system_prompt(policy, "jump", "2")
    assert "Never say, spell or rhyme the word" in s and "2nd grade" in s
    assert policy.ai_disclosure.disclosure_reply in s


# ---- the screen ---------------------------------------------------------------------

class NoPictures:
    def find(self, subject, category=None):
        raise AssertionError("no picture lookups while a word is up")


def test_the_word_stays_on_the_screen_between_turns():
    display = FakeDisplay()
    show = PictureShow(display, NoPictures())
    show.word("jump")
    assert len(display.shown) == 1
    show.show()      # Chatbox speaks...
    show.clear()     # ...and stops: the word stays up
    assert display.blanks == 0
    show.word("jump")  # the same word again draws nothing new
    assert len(display.shown) == 1
    show.word(None)  # the game is over
    assert display.blanks == 1


def test_no_pictures_while_a_word_is_up():
    class Octopus:
        def classify(self, question, history, policy):
            return Classification("allow", subject="octopus", picture_category="animal")

    show = PictureShow(FakeDisplay(), NoPictures())
    show.word("jump")
    WatchingClassifier(Octopus(), show).classify("octopus", [], None)
    assert not show.looking


def test_the_word_fits_the_screen():
    for word in ["up", "magnificent"]:
        image = word_image(word, (320, 240))
        assert image.size == (320, 240)
        # Dark pixels only well inside the edges: the word fits.
        box = image.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()
        assert box[0] > 0 and box[2] < 320


def test_the_screen_is_told_through_the_pipeline(policy, make):
    p, _ = make(policy)
    p.show_word = lambda w: (_ for _ in ()).throw(RuntimeError("screen broke"))
    assert p.ask(START).text == START_REPLY  # a broken screen never fails a question


def test_the_word_is_told_at_the_third_wrong_try_not_before(policy, make):
    p, _ = make(policy, Judge({"dog": "try_again"}))
    p.ask(START)
    word = p.game.word
    assert p.ask("dog").text == "(try_again)"
    assert p.ask("dog").text == "(try_again)"
    a = p.ask("dog")
    assert a.text == f"{tell_word(word)} {NEXT_WORD}" and p.game.index == 1
