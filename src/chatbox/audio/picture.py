"""The screen: a picture of what was asked about, while Chatbox answers (PLAN.md D28).

Pictures only, never text -- neither kid reads fluently. The screen supplements the
spoken answer and is never needed to understand it, so every failure here is silent.
The panel is mounted landscape (320x240), which is the shape most lead images are.

Built the same way as `chatbox.audio.light`: two wrappers around things the turn
already uses, so `chatbox/voice.py` and the pipeline stay free of hardware.

    WatchingClassifier  wraps the guardrail classifier. It sees the subject the
                        classifier reports and starts the lookup right there, while
                        the answer is still being written.
    PicturedSpeaker     wraps the `Speaker`. The picture goes up when playback starts
                        and comes down when it ends, so the screen always matches
                        what's happening.

Timing (requirement 3, D4's 5 s budget): the lookup runs on its own thread and the
speaker never waits for it. `show()` takes whatever is ready at that instant; anything
still in flight is shown when it arrives, if the turn it belongs to is still speaking.
A picture is late or absent, never a delay.

Between turns the screen is blank. An idle picture was considered and dropped: a
screen that is lit only while Chatbox speaks tells a child, without a word, which of
the two things on the front panel is the one that is talking -- and it can't sit
showing a jellyfish half an hour after anyone asked about one.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterable, Protocol

from chatbox.pictures import Picture

_log = logging.getLogger(__name__)

# One worker: lookups are per turn and strictly serial, and a single thread means a
# stalled fetch can never pile up threads on a Pi 3B.
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chatbox-picture")


class Display(Protocol):
    def show(self, image) -> None:
        """Put a PIL image on the screen."""
        ...

    def blank(self) -> None: ...

    def close(self) -> None: ...


class NoDisplay:
    """Used when no screen is wired, so nothing else needs to check."""

    def show(self, image) -> None:
        pass

    def blank(self) -> None:
        pass

    def close(self) -> None:
        pass


class PictureShow:
    """Holds the lookup for the current turn and drives the display.

    Nothing here raises. A screen that fails takes the picture with it and nothing
    else: the child still hears the whole answer.
    """

    def __init__(self, display: Display, finder) -> None:
        self.display = display
        self.finder = finder
        self.last: Picture | None = None      # what is on the screen right now
        # What this turn put on the screen, kept after it is cleared so --verbose can
        # report it: by the time a spoken turn prints its steps, the speaker has
        # already blanked the screen.
        self.previous: Picture | None = None
        self._lock = threading.Lock()
        # The lookup in flight. Nothing waits on it -- it is kept as a handle on the
        # turn's work (the tests wait on it, and a cancel would need it).
        self._pending: Future | None = None
        self._result: Picture | None = None   # set under the lock the moment it arrives
        self._turn = 0          # bumped per turn, so a late picture knows if it's stale
        self._showing = False   # True between show() and clear(): is anyone speaking?

    # -- called from the classifier, as soon as the subject is known ----------

    def wanted(self, subject: str | None) -> None:
        with self._lock:
            self._turn += 1
            self._pending = self._result = self.previous = None
            if not subject:
                return
            turn = self._turn
            self._pending = _POOL.submit(self._find, subject, turn)

    def _find(self, subject: str, turn: int):
        try:
            picture = self.finder.find(subject)
        except Exception as e:  # noqa: BLE001 - the finder shouldn't raise; if it does, no picture
            _log.debug("picture lookup for %r failed: %s", subject, e)
            return None
        with self._lock:
            if turn != self._turn:
                return None       # a newer question has already replaced this one
            # The result is published and the decision taken inside one lock, so this
            # can't cross with show(): either show() has already set _showing (and this
            # shows the picture itself), or it is about to and will find _result set.
            self._result = picture
            late = self._showing
        if late and picture is not None:
            self._put(picture, turn)
        return picture

    @property
    def looking(self) -> bool:
        """A lookup is still in flight, so a picture may yet appear. Only for
        reporting -- nothing waits on this."""
        pending = self._pending
        return pending is not None and not pending.done()

    # -- called from the speaker ---------------------------------------------

    def show(self) -> None:
        """Playback is starting. Show whatever is ready; never wait for the rest."""
        with self._lock:
            self._showing = True
            picture, turn = self._result, self._turn
        if picture is not None:
            self._put(picture, turn)

    def clear(self) -> None:
        """The turn is over: the screen goes blank so it never outlives the answer."""
        with self._lock:
            self._showing = False
            self.last = self._result = None
        try:
            self.display.blank()
        except Exception as e:  # noqa: BLE001
            _log.debug("couldn't blank the screen: %s", e)

    def close(self) -> None:
        try:
            self.display.close()
        except Exception as e:  # noqa: BLE001
            _log.debug("couldn't close the screen: %s", e)

    def _put(self, picture: Picture, turn: int) -> None:
        try:
            self.display.show(picture.image)
        except Exception as e:  # noqa: BLE001 - a dead screen must not end the turn
            _log.debug("couldn't show the picture of %r: %s", picture.subject, e)
            return
        with self._lock:
            if turn == self._turn and self._showing:
                self.last = self.previous = picture


class WatchingClassifier:
    """An `InputClassifier` that also starts the picture lookup. Classifies unchanged.

    Only an "allow" starts a lookup: a question that was redirected to a parent or
    refused gets the canned reply and a blank screen, which is the behaviour to want
    anyway -- nothing about a blocked question should be illustrated.
    """

    def __init__(self, classifier, show: PictureShow) -> None:
        self.classifier, self.show = classifier, show

    def classify(self, question, history, policy):
        classification = self.classifier.classify(question, history, policy)
        try:
            self.show.wanted(classification.subject
                             if classification.decision == "allow" else None)
        except Exception as e:  # noqa: BLE001 - the screen never fails a classification
            _log.debug("couldn't start the picture lookup: %s", e)
        return classification


class PicturedSpeaker:
    """A `Speaker` that also drives a `PictureShow`. Delegates all audio unchanged."""

    # Cues that end a turn without an answer: nothing to illustrate, so blank.
    _CLEARING = {"cancel", "error", "listening"}

    def __init__(self, speaker, show: PictureShow) -> None:
        self.speaker, self.show = speaker, show

    def cue(self, name: str, **kwargs) -> None:
        if name in self._CLEARING:
            self.show.clear()
        self.speaker.cue(name, **kwargs)

    def play(self, audio: Iterable[bytes], sample_rate: int) -> None:
        self.show.show()
        try:
            self.speaker.play(audio, sample_rate)
        finally:
            # Blank even if playback raised, so the screen never outlasts the voice.
            self.show.clear()

    def close(self) -> None:
        self.show.close()
        # Not every speaker has one (the laptop's doesn't); pass it on where it does.
        closing = getattr(self.speaker, "close", None)
        if closing is not None:
            closing()
