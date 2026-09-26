"""The animal guessing game: switching modes, the secret animal, and what the answering
model and the log see, with the models mocked."""

import random

import pytest

from chatbox.animal_game import (
    ANIMALS,
    START_REPLY,
    AnimalGame,
    game_system_prompt,
    names_animal,
    stop_reply,
    with_article,
)
from chatbox.guardrails import Classification, ModelClassifier, classifier_system_prompt
from chatbox.pipeline import Pipeline
from tests.conftest import WED_NOON, FakeProvider, PassAll

START = "I want to play the animal guessing game"
STOP = "I'm done playing animal guessing game"


class Modes:
    """Allows everything, and hears a mode change in the phrases above."""

    MODES = {START: "start_animal_game", "play again": "start_animal_game",
             STOP: "stop_game"}

    def classify(self, question, history, policy):
        return Classification("allow", detail="test", mode_change=self.MODES.get(question))


@pytest.fixture
def make(counter):
    def _make(policy, classifier=None, provider=None):
        provider = provider or FakeProvider("No, it can't fly.")
        p = Pipeline(policy, provider, counter, clock=lambda: WED_NOON,
                     classifier=classifier or Modes(), output_checker=PassAll(),
                     rng=random.Random(1))
        return p, provider
    return _make


def steps(a):
    return [(s["step"], s["decision"]) for s in a.steps]


def system_for(provider, question):
    """The system prompt the answer to `question` was asked with. Found by question, since
    an answer thrown away on a mode switch can land in `calls` after the next one."""
    return next(system for system, messages in provider.calls if messages[-1].text == question)


def test_starts_in_question_answering_mode(policy, make):
    p, provider = make(policy)
    p.ask("Why is the sky blue?")
    assert p.game is None
    assert provider.calls[0][0] == p.system_prompt


def test_asking_to_play_starts_a_game_with_no_model_answer(policy, make):
    p, _ = make(policy)
    a = p.ask(START)
    assert a.text == START_REPLY and a.answered_by == "canned"
    assert p.game is not None and p.game.animal in ANIMALS
    assert ("game", "start") in steps(a) and ("generate", "discarded") in steps(a)
    assert f"secret animal: {p.game.animal}" in next(
        s["detail"] for s in a.steps if s["step"] == "game")


def test_game_questions_are_answered_about_the_secret_animal(policy, make):
    p, provider = make(policy)
    p.ask(START)
    animal = p.game.animal
    a = p.ask("Does it fly?")
    assert a.text == "No, it can't fly." and a.answered_by == "model"
    assert ("game", "play") in steps(a) and ("output_check", "pass") in steps(a)
    system, messages = next(c for c in provider.calls if c[1][-1].text == "Does it fly?")
    assert system == game_system_prompt(policy, animal)
    assert f"Your secret animal is: {animal}." in system
    # The game sees only itself, starting with the request that began it.
    assert [m.text for m in messages] == [START, START_REPLY, "Does it fly?"]


def test_the_animal_stays_the_same_all_game(policy, make):
    p, provider = make(policy)
    p.ask(START)
    animal = p.game.animal
    for q in ["Is it a mammal?", "Does it live in the jungle?", "Is it a lion?"]:
        p.ask(q)
    assert all(f"Your secret animal is: {animal}." in system_for(provider, q)
               for q in ["Is it a mammal?", "Does it live in the jungle?", "Is it a lion?"])


def test_game_answers_still_go_through_the_guardrails(policy, counter):
    class RefuseInGame(Modes):
        def classify(self, question, history, policy):
            if question == "Does it eat people?":
                return Classification("refuse", "violence_and_weapons", "test")
            return super().classify(question, history, policy)

    p = Pipeline(policy, FakeProvider(), counter, clock=lambda: WED_NOON,
                 classifier=RefuseInGame(), output_checker=PassAll())
    p.ask(START)
    a = p.ask("Does it eat people?")
    assert a.text == policy.canned_replies.blocked_topic
    assert p.game is not None  # a refused question doesn't end the game


def test_saying_done_goes_back_to_question_answering(policy, make):
    p, provider = make(policy)
    p.ask(START)
    animal = p.game.animal
    p.ask("Does it fly?")
    a = p.ask(STOP)
    assert a.text == stop_reply(animal) and animal in a.text and a.answered_by == "canned"
    assert ("game", "stop") in steps(a)
    assert p.game is None
    p.ask("Why is the sky blue?")
    assert system_for(provider, "Why is the sky blue?") == p.system_prompt


def test_stop_outside_a_game_is_just_a_question(policy, make):
    p, provider = make(policy)
    a = p.ask(STOP)
    assert a.answered_by == "model" and p.game is None
    assert "game" not in [s["step"] for s in a.steps]


def test_play_again_picks_a_new_animal(policy, make):
    p, _ = make(policy)
    p.ask(START)
    first = p.game.animal
    p.ask("Is it a lion?")
    a = p.ask("play again")
    assert a.text == START_REPLY
    assert p.game.animal != first
    assert p.game.history[0].text == "play again"


def test_a_new_round_never_repeats_the_last_animal():
    rng = random.Random(0)
    for animal in ANIMALS:
        assert AnimalGame.start(rng, None, AnimalGame(animal)).animal != animal


def test_classifier_hears_the_whole_session(policy, make):
    seen = []

    class Records(Modes):
        def classify(self, question, history, policy):
            seen.append([m.text for m in history])
            return super().classify(question, history, policy)

    p, _ = make(policy, classifier=Records())
    p.ask("Why is the sky blue?")
    p.ask(START)
    p.ask("Does it fly?")
    assert seen[-1][0] == "Why is the sky blue?"


@pytest.mark.parametrize("value, expected", [
    ("start_animal_game", "start_animal_game"),
    ("start_reading_game", "start_reading_game"),
    ("stop_game", "stop_game"),
    ("stop_animal_game", None),
    ("none", None),
    ("dance", None),
    (None, None),
])
def test_classifier_mode_change_never_fails_a_question(policy, value, expected):
    data = {"decision": "allow", "topic_id": "none", "reason": "fine"}
    if value is not None:
        data["mode_change"] = value
    c = ModelClassifier(FakeProvider(structured=data)).classify("hi", [], policy)
    assert c.decision == "allow" and c.mode_change == expected


def test_classifier_is_told_about_the_game(policy):
    s = classifier_system_prompt(policy)
    assert "start_animal_game" in s and "start_reading_game" in s and "stop_game" in s


def test_game_prompt_keeps_the_policy_rules(policy):
    s = game_system_prompt(policy, "octopus")
    assert policy.persona.name in s
    assert f"{policy.household_age}-year-old" in s
    assert policy.ai_disclosure.disclosure_reply in s
    assert "an octopus" in s and "# Safety" in s


def test_articles():
    assert with_article("owl") == "an owl" and with_article("dog") == "a dog"


class Says:
    """Answers each question with its own scripted text."""

    name, model = "fake", "fake-model"

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def generate(self, system, messages):
        from chatbox.providers.base import ModelReply
        self.calls.append((system, messages))
        return ModelReply(self.answers.get(messages[-1].text, "No!"), self.model)


def test_the_round_ends_when_the_animal_is_guessed(policy, make):
    p, _ = make(policy)
    p.ask(START)
    animal = p.game.animal
    p.provider = provider = Says({f"Is it a {animal}?": f"Yes! It was a {animal}!"})
    a = p.ask(f"Is it a {animal}?")
    assert ("game", "over") in steps(a) and p.game is None
    # Anything but "play again" is answered normally.
    p.ask("Why is the sky blue?")
    assert system_for(provider, "Why is the sky blue?") == p.system_prompt


def test_giving_up_ends_the_round_too(policy, make):
    p, _ = make(policy)
    p.ask(START)
    animal = p.game.animal
    p.provider = Says({"I give up": f"It was {with_article(animal)}! Say play again."})
    p.ask("I give up")
    assert p.game is None


def test_play_again_after_a_round_starts_a_new_one(policy, make):
    p, _ = make(policy)
    p.ask(START)
    first = p.game.animal
    p.provider = Says({f"Is it a {first}?": f"Yes, a {first}!"})
    p.ask(f"Is it a {first}?")
    a = p.ask("play again")
    assert a.text == START_REPLY and p.game is not None and p.game.animal != first


def test_a_wrong_guess_keeps_the_round_going(policy, make):
    p, _ = make(policy)
    p.ask(START)
    p.provider = Says({"Is it a unicorn?": "No! Keep guessing!"})
    p.ask("Is it a unicorn?")
    assert p.game is not None


def test_a_blocked_answer_that_names_the_animal_doesnt_end_the_round(policy, counter):
    from tests.test_pipeline_and_log import FailAll
    p = Pipeline(policy, FakeProvider(), counter, clock=lambda: WED_NOON,
                 classifier=Modes(), output_checker=FailAll(), rng=random.Random(1))
    p.ask(START)
    p.provider = Says({"Is it a x?": f"Yes, a {p.game.animal}!"})
    p.ask("Is it a x?")
    assert p.game is not None  # the child never heard the name


@pytest.mark.parametrize("answer, animal, named", [
    ("Yes! It was a cat!", "cat", True),
    ("Cats are great, and that's it!", "cat", True),
    ("It was a FOX!", "fox", True),
    ("Two foxes!", "fox", True),
    ("No, it isn't a caterpillar.", "cat", False),
    ("No! Keep guessing!", "ant", False),
    ("It has antlers.", "ant", False),
])
def test_names_animal(answer, animal, named):
    assert names_animal(answer, animal) is named


# ---- daily limits -------------------------------------------------------------------

from chatbox.animal_game import GAME_ID  # noqa: E402
from chatbox.policy import Policy  # noqa: E402


class Records(Modes):
    def __init__(self):
        self.questions = []

    def classify(self, question, history, policy):
        self.questions.append(question)
        return super().classify(question, history, policy)


def with_limits(policy_data, questions=40, games=5, enabled=True):
    policy_data["limits"]["daily_questions"] = questions
    policy_data.setdefault("games", {})["animal_guessing"] = {"enabled": enabled,
                                                            "daily_games": games}
    return Policy.model_validate(policy_data)


def finish_round(p):
    """Guess the animal, so the round ends and counts as a finished game."""
    animal = p.game.animal
    p.provider = Says({f"Is it a {animal}?": f"Yes! It was a {animal}!"})
    return p.ask(f"Is it a {animal}?")


def test_game_questions_dont_count_toward_the_days_questions(policy, make):
    p, _ = make(policy)
    p.ask(START)
    p.ask("Does it fly?")
    p.ask("Is it a mammal?")
    p.ask(STOP)
    assert p.counter.get("2026-09-16") == 0
    p.ask("Why is the sky blue?")
    assert p.counter.get("2026-09-16") == 1


def test_a_finished_game_counts_and_a_stopped_one_doesnt(policy, make):
    p, _ = make(policy)
    p.ask(START)
    p.ask(STOP)
    assert p.counter.games("2026-09-16", GAME_ID) == 0
    p.ask(START)
    finish_round(p)
    assert p.counter.games("2026-09-16", GAME_ID) == 1
    assert p.counter.get("2026-09-16") == 0  # the winning guess was a game question too


def test_no_more_games_once_todays_are_finished(policy_data, make):
    policy = with_limits(policy_data, games=1)
    p, _ = make(policy)
    p.ask(START)
    finish_round(p)
    a = p.ask("play again")
    assert a.text == policy.games.animal_guessing.unavailable_reply and p.game is None
    assert ("game", "unavailable") in steps(a)
    assert p.counter.get("2026-09-16") == 0
    # Questions still work.
    assert p.ask("Why is the sky blue?").answered_by == "model"


def test_zero_games_per_day_means_no_games(policy_data, make):
    policy = with_limits(policy_data, games=0)
    p, _ = make(policy)
    assert p.ask(START).text == policy.games.animal_guessing.unavailable_reply


def test_a_game_that_is_turned_off_cant_start(policy_data, make):
    policy = with_limits(policy_data, enabled=False)
    p, _ = make(policy)
    a = p.ask(START)
    assert a.text == policy.games.animal_guessing.unavailable_reply and p.game is None
    assert "turned off" in next(s["detail"] for s in a.steps if s["step"] == "game")


def test_turning_the_game_off_ends_one_in_progress(policy, make):
    from chatbox.policy_store import PolicyStore
    p, provider = make(policy)
    p.store = PolicyStore(policy)
    p.ask(START)
    data = policy.model_dump(mode="json")
    data["games"]["animal_guessing"]["enabled"] = False
    p.store.save(data)
    p.ask("Does it fly?")
    assert p.game is None
    assert system_for(provider, "Does it fly?") == p.system_prompt


def test_games_carry_on_after_the_days_questions_are_used_up(policy_data, make):
    policy = with_limits(policy_data, questions=1)
    p, provider = make(policy)
    p.ask("Why is the sky blue?")
    assert p.ask("Why is grass green?").text == policy.canned_replies.daily_limit_reached
    # A request to play still starts a game, and its questions are answered.
    assert p.ask(START).text == START_REPLY
    a = p.ask("Does it fly?")
    assert a.answered_by == "model" and ("limits", "allow") in steps(a)
    finish_round(p)
    # Once the game is over, it's back to the daily-limit reply.
    a = p.ask("Why is grass green?")
    assert a.text == policy.canned_replies.daily_limit_reached
    assert p.counter.get("2026-09-16") == 1


def test_over_the_cap_only_the_classifier_runs(policy_data, make):
    policy = with_limits(policy_data, questions=0)
    p, provider = make(policy)
    a = p.ask("Why is the sky blue?")
    assert a.text == policy.canned_replies.daily_limit_reached
    assert steps(a) == [("limits", "games_only"), ("classify", "allow"), ("limits", "deny")]
    assert provider.calls == []  # no answer was asked for
    assert p.history == []  # and it doesn't join the conversation


def test_over_the_cap_with_no_games_left_no_model_is_called(policy_data, make):
    policy_data["games"] = {"reading": {"daily_games": 0}}
    policy = with_limits(policy_data, questions=0, games=0)
    classifier = Records()
    p, provider = make(policy, classifier=classifier)
    a = p.ask(START)
    assert a.text == policy.canned_replies.daily_limit_reached
    assert classifier.questions == [] and provider.calls == []


def test_the_schedule_still_applies_to_games(policy, counter):
    from tests.conftest import at
    p = Pipeline(policy, FakeProvider(), counter, clock=lambda: at(2026, 9, 16, 21),
                 classifier=Modes(), output_checker=PassAll())
    assert p.ask(START).text == policy.canned_replies.outside_schedule
