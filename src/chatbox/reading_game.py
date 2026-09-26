"""The reading game: Chatbox shows a word on the screen, and the child reads it aloud.

It's a `chatbox.games.Game`. A game is `WORDS_PER_GAME` words at the reading level a
parent sets on the settings page (kindergarten to 5th grade). The words are picked here,
in code, from the lists below; the model only judges each try and says something kind.

Speech recognition often mishears a single word ("two" for "to"), and many of these words
turn up in any sentence ("the", "it"), so the answer can't be checked by looking for the
word in it. Instead the model returns a verdict with its reply (structured output):
read correctly, a wrong try, gave up, or not a try at all. When to tell the child the
word is decided here, not by the model (which told it too soon): when they give up, or
at their `MAX_WRONG_TRIES`th wrong try, in fixed words. A right answer or a told word
moves on to the next word; after the last one the game is over.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import ClassVar

from chatbox.games import Game, GameReply, Outcome
from chatbox.policy import Policy
from chatbox.prompt import SAFETY_SECTION, honesty_section, persona_line
from chatbox.providers.base import ChatMessage, ModelProvider, ModelReply

GAME_ID = "reading"
WORDS_PER_GAME = 5
# After this many wrong tries at one word, the child is told it.
MAX_WRONG_TRIES = 3

GRADES = ["K", "1", "2", "3", "4", "5"]
GRADE_NAMES = {"K": "kindergarten", "1": "1st grade", "2": "2nd grade", "3": "3rd grade",
               "4": "4th grade", "5": "5th grade"}

# Kindergarten to 3rd grade are mostly Dolch sight words for that grade, with a few
# simple nouns. 4th and 5th grade are common words of that level. Single letters ("a",
# "I") are left out: speech recognition rarely hears one on its own.
WORDS = {
    "K": ["and", "big", "blue", "can", "come", "down", "find", "for", "funny", "go", "help",
          "here", "in", "is", "it", "jump", "little", "look", "make", "me", "my", "not",
          "one", "play", "red", "run", "said", "see", "the", "three", "up", "we", "you",
          "yellow", "cat", "dog", "sun", "hat", "pig", "bed", "cup", "fish", "ball", "car"],
    "1": ["after", "again", "any", "ask", "by", "could", "every", "fly", "from", "give",
          "going", "had", "has", "her", "him", "his", "how", "just", "know", "let", "live",
          "may", "of", "old", "once", "open", "over", "put", "round", "some", "stop", "take",
          "thank", "them", "then", "think", "walk", "were", "when", "frog", "ship", "tree"],
    "2": ["always", "around", "because", "been", "before", "best", "both", "buy", "call",
          "cold", "does", "fast", "first", "five", "found", "gave", "goes", "green", "made",
          "many", "off", "pull", "read", "right", "sing", "sleep", "tell", "their", "these",
          "those", "upon", "use", "very", "wash", "which", "why", "wish", "work", "would",
          "write", "your", "rabbit", "garden"],
    "3": ["about", "better", "bring", "carry", "clean", "done", "draw", "drink", "eight",
          "fall", "far", "full", "grow", "hold", "hurt", "keep", "kind", "laugh", "light",
          "long", "much", "myself", "never", "only", "pick", "seven", "shall", "show", "small",
          "start", "today", "together", "warm", "animal", "picture", "morning", "family",
          "window", "birthday", "different"],
    "4": ["adventure", "although", "answer", "beautiful", "believe", "breakfast", "brought",
          "caught", "character", "country", "discover", "enough", "favorite", "forest",
          "friendship", "important", "instead", "island", "journey", "language", "machine",
          "measure", "minute", "neighbor", "ocean", "question", "remember", "science",
          "special", "straight", "surprise", "thought", "through", "weather", "whole"],
    "5": ["accomplish", "ancient", "atmosphere", "boundary", "certain", "community",
          "continent", "curious", "delicious", "disappear", "electricity", "environment",
          "experiment", "explanation", "familiar", "fortunate", "government", "imagination",
          "independent", "investigate", "magnificent", "mysterious", "necessary", "observe",
          "opportunity", "particular", "population", "probably", "recognize", "separate",
          "temperature", "tremendous", "unusual", "vocabulary"],
}

START_REPLY = "Let's read! Look at the screen and read the word out loud."
STOP_REPLY = "Okay, great reading! Now you can ask me anything."
NEXT_WORD = "Here's the next word."
LAST_WORD = "That was the last word. Great reading!"

RESULTS = {"correct": "done", "try_again": "wrong", "gave_up": "done", "other": "continue"}


def tell_word(word: str) -> str:
    return f"That word is {word}. Good try!"


def verdict_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "result": {"type": "string", "enum": list(RESULTS)},
            "reply": {"type": "string"},
        },
        "required": ["result", "reply"],
        "additionalProperties": False,
    }


@dataclass
class ReadingGame:
    id: ClassVar[str] = GAME_ID
    start_reply: ClassVar[str] = START_REPLY
    words: list[str]
    grade: str
    index: int = 0          # which word is on the screen
    wrong_tries: int = 0    # at the current word
    history: list[ChatMessage] = field(default_factory=list)

    @classmethod
    def start(cls, rng: random.Random, policy: Policy,
              previous: Game | None = None) -> ReadingGame:
        """A new game at the policy's reading level, avoiding the last game's words
        while there are enough others."""
        grade = policy.games.reading.grade
        seen = set(previous.words) if isinstance(previous, ReadingGame) else set()
        fresh = [w for w in WORDS[grade] if w not in seen]
        pool = fresh if len(fresh) >= WORDS_PER_GAME else WORDS[grade]
        return cls(rng.sample(pool, WORDS_PER_GAME), grade)

    @property
    def word(self) -> str:
        return self.words[self.index]

    @property
    def screen_word(self) -> str:
        return self.word

    def stop_reply(self) -> str:
        return STOP_REPLY

    def system_prompt(self, policy: Policy) -> str:
        return reading_system_prompt(policy, self.word, self.grade)

    def answer(self, provider: ModelProvider, system: str,
               messages: list[ChatMessage]) -> GameReply:
        structured = provider.generate_structured(system, messages, verdict_schema())
        result, reply = structured.data.get("result"), structured.data.get("reply")
        if result not in RESULTS or not isinstance(reply, str):
            raise ValueError(f"reading game verdict is unusable: {structured.data!r}")
        outcome = RESULTS[result]
        if result == "gave_up" or (result == "try_again"
                                   and self.wrong_tries + 1 >= MAX_WRONG_TRIES):
            reply, outcome = tell_word(self.word), "done"
        elif not reply.strip():
            raise ValueError(f"reading game verdict has no reply: {structured.data!r}")
        u = structured.usage
        model_reply = ModelReply(
            reply.strip(), u.model if u else provider.model, stop_reason=result,
            input_tokens=u.input_tokens if u else None,
            output_tokens=u.output_tokens if u else None,
            cached_input_tokens=u.cached_input_tokens if u else 0)
        return GameReply(model_reply, outcome)

    def advance(self, outcome: Outcome) -> tuple[str, bool]:
        if outcome == "wrong":
            self.wrong_tries += 1
        if outcome != "done":
            return "", False
        if self.index + 1 >= len(self.words):
            return LAST_WORD, True
        self.index += 1
        self.wrong_tries = 0
        # The model only needs this word's tries. The exchange that finished the last
        # word is added after this, and reads naturally: "...Here's the next word."
        self.history.clear()
        return NEXT_WORD, False

    def progress(self) -> str:
        return f"word {self.index + 1} of {len(self.words)}: {self.word}"


def reading_system_prompt(policy: Policy, word: str, grade: str) -> str:
    return f"""{persona_line(policy)}

# The reading game
You are helping a child practice reading at the {GRADE_NAMES[grade]} level. A word is shown on a screen, and the child reads it out loud. Their words reach you through speech recognition.
- The word on the screen now is: "{word}".
- Never say, spell or rhyme the word before the child has read it. Hearing it would read it for them.
- Speech recognition often mishears a single word. Count it as read correctly if it is the word, sounds the same ("two" for "to"), or is plainly the word misheard. A different word is a wrong try.

Set result and write reply:
- "correct": they read it. Cheer briefly and say the word, like "Yes! That says jump!"
- "try_again": they read a different word. Say kindly that it's not quite right, and give one small hint that doesn't say the word, such as its first sound or how many letters it has.
- "gave_up": they ask for the answer, say they don't know, or want to skip. Leave reply empty: Chatbox tells them the word.
- "other": only for a question or chat that plainly isn't reading ("why is the sky blue?"). Ask them to read the word on the screen, and say they can say "I'm done playing" to stop.
Any single word or short phrase the child says is a try at reading the word, even a word that looks nothing like it: "correct" if it's the word, otherwise "try_again".
Don't say what happens next; the next word, if there is one, is announced for you.

# How to answer
- Your reply will be read aloud, so use plain spoken words only: no lists, emoji, markdown, or symbols.
- Use at most 2 short sentences.

{honesty_section(policy)}

{SAFETY_SECTION}"""
