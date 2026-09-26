"""The animal guessing game: Chatbox thinks of an animal, and the children ask yes-or-no
questions ("does it fly?") until they guess it.

Chatbox starts in question-answering mode. The guardrail classifier, which already runs
on every question, notices "I want to play the animal guessing game" and "I'm done
playing" (`Classification.mode_change`), and the pipeline switches mode. Starting and
stopping get fixed replies with no model call, so the switch is instant.

A round ends when an answer names the secret animal (`names_animal`): the prompt forbids
that until the child guesses it or gives up. Chatbox then goes back to answering
questions, and "play again" starts a new round from there.

The secret animal is picked here, in code, and written into the game's system prompt, so
it can't drift from one answer to the next. Answers in the game go through the same
guardrails as any other answer.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from chatbox.policy import Policy
from chatbox.prompt import SAFETY_SECTION, honesty_section, persona_line
from chatbox.providers.base import ChatMessage

# Animals a young child knows, each with plain, well-known traits so that yes-or-no
# answers about them are clear.
ANIMALS = [
    "dog", "cat", "cow", "horse", "pig", "sheep", "chicken", "duck", "rabbit", "mouse",
    "lion", "tiger", "elephant", "giraffe", "zebra", "monkey", "gorilla", "bear", "panda",
    "kangaroo", "koala", "camel", "fox", "wolf", "deer", "squirrel", "bat",
    "penguin", "owl", "eagle", "parrot", "flamingo", "shark", "dolphin", "whale",
    "octopus", "jellyfish", "crab", "turtle", "frog", "snake", "crocodile", "butterfly",
    "bee", "ant", "spider", "snail",
]

START_REPLY = ("Okay, let's play! I'm thinking of an animal. Ask me yes or no questions, "
               "like does it fly, and try to guess what it is.")


def stop_reply(animal: str) -> str:
    return (f"Okay, that was fun! I was thinking of {with_article(animal)}. "
            "Now you can ask me anything.")


def names_animal(answer: str, animal: str) -> bool:
    """Whether the answer says the animal's name ("cat", "Cats"), which means the round is
    over. Whole words only, so "cat" isn't found in "caterpillar"."""
    return re.search(rf"\b{re.escape(animal)}(e?s)?\b", answer, re.IGNORECASE) is not None


def with_article(noun: str) -> str:
    return f"{'an' if noun[0] in 'aeiou' else 'a'} {noun}"


@dataclass
class AnimalGame:
    """One round: the secret animal and the exchanges since it was picked."""

    animal: str
    history: list[ChatMessage] = field(default_factory=list)

    @classmethod
    def start(cls, rng: random.Random, previous: str | None = None) -> AnimalGame:
        """A new round, never with the animal of the round before."""
        return cls(rng.choice([a for a in ANIMALS if a != previous]))


def game_system_prompt(policy: Policy, animal: str) -> str:
    a = policy.answers
    return f"""{persona_line(policy)}

# The animal guessing game
Right now you are playing the animal guessing game with a {policy.household_age}-year-old. You have picked a secret animal, and the child asks yes-or-no questions to work out what it is.
- Your secret animal is: {animal}. It never changes during the game.
- Answer each question truthfully about {with_article(animal)}: "Yes!", "No!", "Maybe", "Sometimes", or "I don't know", plus at most a few words, like "No, it can't fly."
- Answer the question the way a child means it, and give a clear "Yes!" or "No!" whenever you can. Say "Maybe" or "Sometimes" only when it really differs from one animal of that kind to another. Say "I don't know" when you really don't know. Never make things up.
- Never say the animal's name, or a clue that gives it away, until the child guesses it or gives up.
- When the child guesses it (or a name that means the same animal), cheer, say what it was, and tell them they can say "play again" for a new animal, or ask you anything.
- A wrong guess is just "No!", perhaps with "Keep guessing!"
- If the child gives up or asks for the answer, tell them kindly what it was, and that they can say "play again" for a new animal, or ask you anything.
- If the child asks for a hint, give one small true clue, never the animal's most famous feature (not an elephant's trunk or a zebra's stripes).
- If the question isn't a yes-or-no question about the animal, gently remind them to ask one, like "Does it have fur?"
- If the child talks about something else, say you're playing the animal guessing game, and they can say "I'm done playing" to go back to asking questions.

# How to answer
- Your answer will be read aloud, so use plain spoken words only: no lists, emoji, markdown, or symbols.
- Use at most {a.max_sentences} short sentences.

{honesty_section(policy)}

{SAFETY_SECTION}"""
