"""What the pipeline needs from a game (chatbox.animal_game, chatbox.reading_game).

Chatbox starts in question-answering mode. The guardrail classifier notices a request to
play a game, or to stop (`Classification.mode_change`), and the pipeline switches mode.
While a game is on, its own prompt answers the child, with the same guardrails around
it. Each game's answer says whether it finished a round; when the game is over, Chatbox
goes back to answering questions, and the game counts toward its daily limit in the
policy's `games` section (keyed by the game's `id`).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal, Protocol

from chatbox.policy import Policy
from chatbox.providers.base import ChatMessage, ModelProvider, ModelReply

# What one answer did to the game:
#   "continue"  nothing changed (a question in the animal game, a chat in the reading game)
#   "wrong"     a wrong try at the current round (the reading game counts these)
#   "done"      the round is over (the animal was guessed, the word was read or told)
Outcome = Literal["continue", "wrong", "done"]


@dataclass(frozen=True)
class GameReply:
    reply: ModelReply  # its text is what the child hears, before any closing words
    outcome: Outcome


class Game(Protocol):
    id: str  # its key in the policy's `games` section and in the daily game counts
    history: list[ChatMessage]  # this game's exchanges, for the answering model

    @classmethod
    def start(cls, rng: random.Random, policy: Policy, previous: Game | None) -> Game:
        """A new game. `previous` is the last game of this kind, to avoid repeats."""
        ...

    @property
    def start_reply(self) -> str: ...

    def stop_reply(self) -> str: ...

    @property
    def screen_word(self) -> str | None:
        """A word for the screen to show the whole time the game is on, or None."""
        ...

    def system_prompt(self, policy: Policy) -> str: ...

    def answer(self, provider: ModelProvider, system: str,
               messages: list[ChatMessage]) -> GameReply:
        """The model's answer to the child's latest message. Runs on a worker thread, so
        it must not change the game: `advance` does that, once the answer passed."""
        ...

    def advance(self, outcome: Outcome) -> tuple[str, bool]:
        """Apply an answer the child is about to hear. Returns words to add after it
        ("Here's the next word.") and whether the game is now over."""
        ...

    def progress(self) -> str:
        """For the log: where the game is ("secret animal: owl", "word 2 of 5: jump")."""
        ...


def games_by_mode() -> dict[str, type]:
    """The classifier's `mode_change` for starting each game, and that game's class."""
    from chatbox.animal_game import AnimalGame
    from chatbox.reading_game import ReadingGame

    return {"start_animal_game": AnimalGame, "start_reading_game": ReadingGame}
