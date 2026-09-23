"""Finding a picture of what was asked about (PLAN.md D28, D29).

The subject comes from the guardrail classifier, which already runs on every question,
so no extra model call and no extra latency. Given a subject, this looks up the
Wikipedia article for it and takes that article's lead image (the MediaWiki API's
`pageimage`), scaled for the 240x320 screen.

Deliberately *not* a Wikimedia Commons free-text image search: that ranks every file
anyone has uploaded against a word, while an article's lead image is about the article.

Everything here is best-effort. No article, no image, a slow network or any error at
all means no picture -- never an exception the caller has to handle, never a retry, and
never anything the child hears. One HTTP round trip per subject, then a disk cache,
including a negative entry so a subject with no picture is asked about once.

    !! UNCHECKED CONTENT (PLAN.md D29) !!
    No guardrail sees this image before a child does. The classifier checks the
    *question*; nothing checks the picture that comes back. Accepted for v1 only
    because Talkbox is family-only and supervised (D17). Before the kid pilot is
    unsupervised -- and certainly before another family's children use it -- this
    needs a parent-approved subject list, edited from the settings page, so that only
    subjects a parent has seen can reach the screen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

_log = logging.getLogger(__name__)

API = "https://en.wikipedia.org/w/api.php"

# Wikimedia asks every client to identify itself, and enforces it: a User-Agent with a
# placeholder contact in it is answered with HTTP 429 from the first request, which
# looks exactly like being rate-limited. If Talkbox is ever published or used by
# another family (PLAN.md D17), put a real project URL or contact address here.
# https://meta.wikimedia.org/wiki/User-Agent_policy
USER_AGENT = ("Talkbox/0.1 (a family voice assistant for two children; "
              "https://www.mediawiki.org/wiki/API:Etiquette)")

# The screen, portrait. Thumbnails are requested a little larger than the panel and
# scaled down here, so a wide image still fills the width.
SCREEN = (240, 320)

# A subject is a noun phrase from the classifier, but it becomes a URL and a filename,
# so it gets treated as untrusted text at both.
_UNSAFE_NAME = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Picture:
    """A decoded image ready for the screen, and where it came from (for --verbose)."""

    image: object      # PIL.Image.Image; typed loosely so this module imports without PIL
    subject: str
    title: str = ""    # the Wikipedia article the lead image belongs to
    source: str = ""   # "wikipedia" or "cache"


class _Deadline:
    """One hard budget shared by every step of a fetch, so two slow steps can't add up
    past the timeout (requirement 3: speech never waits for a picture)."""

    def __init__(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds

    @property
    def left(self) -> float:
        return self.until - time.monotonic()

    def check(self) -> float:
        remaining = self.left
        if remaining <= 0:
            raise TimeoutError("out of time for the picture")
        return remaining


def _get(url: str, deadline: _Deadline) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=deadline.check()) as response:
        # A lead image is tens of kilobytes. Anything much bigger is not something to
        # pull onto a Pi 3B mid-turn, so the read is capped rather than trusted.
        return response.read(4_000_000)


def lead_image_url(subject: str, deadline: _Deadline, width: int = 320) -> tuple[str, str] | None:
    """(image url, article title) for the subject's Wikipedia article, or None.

    One request: `generator=search` finds the best-matching article and `pageimages`
    returns that article's lead image in the same response.
    """
    query = urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": subject, "gsrlimit": "1", "gsrnamespace": "0",
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": str(width),
        "redirects": "1",
    })
    data = json.loads(_get(f"{API}?{query}", deadline).decode("utf-8"))
    pages = data.get("query", {}).get("pages") or []
    if not pages:
        return None  # no article matched the subject
    page = pages[0]
    thumbnail = page.get("thumbnail") or {}
    url = thumbnail.get("source")
    if not url:
        return None  # an article, but no lead image (plenty have none)
    return url, str(page.get("title", ""))


def fit(image, size: tuple[int, int] = SCREEN):
    """Scale to fit inside `size` and centre it on black, so the picture keeps its
    shape and the screen is always filled edge to edge."""
    from PIL import Image

    image = image.convert("RGB")
    image.thumbnail(size, Image.LANCZOS)
    canvas = Image.new("RGB", size, (0, 0, 0))
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return canvas


def _cache_name(subject: str) -> str:
    """A filename per subject: readable where it can be, unique always."""
    slug = _UNSAFE_NAME.sub("-", subject.lower()).strip("-")[:40] or "subject"
    digest = hashlib.sha1(subject.lower().encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


class PictureFinder:
    """Subject in, `Picture` or None out. Never raises.

    `timeout_seconds` is a hard ceiling on everything a single lookup does. Cached
    subjects skip the network entirely, which is what makes a repeated question free.
    """

    def __init__(self, cache_dir: Path | str, timeout_seconds: float = 3.0,
                 size: tuple[int, int] = SCREEN, fetch=None) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout_seconds = timeout_seconds
        self.size = size
        self._fetch = fetch or lead_image_url  # swapped for a fake in the tests

    # -- cache ---------------------------------------------------------------
    # A missing picture is cached too (an empty ".none" file), so "what is a florb?"
    # costs one lookup per subject for the life of the cache, not one per asking.

    def _paths(self, subject: str) -> tuple[Path, Path]:
        name = _cache_name(subject)
        return self.cache_dir / f"{name}.png", self.cache_dir / f"{name}.none"

    def _cached(self, subject: str) -> Picture | None | str:
        """A `Picture`, the string "none" for a cached miss, or None for no entry."""
        image_path, miss_path = self._paths(subject)
        if miss_path.exists():
            return "none"
        if image_path.exists():
            try:
                from PIL import Image

                with Image.open(image_path) as image:
                    return Picture(image.convert("RGB"), subject, source="cache")
            except Exception as e:  # noqa: BLE001 - a corrupt cache file just refetches
                _log.debug("cached picture for %r unreadable: %s", subject, e)
                image_path.unlink(missing_ok=True)
        return None

    def _store(self, subject: str, image) -> None:
        image_path, miss_path = self._paths(subject)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            if image is None:
                miss_path.touch()
            else:
                image.save(image_path, "PNG")
        except Exception as e:  # noqa: BLE001 - an unwritable cache only costs a refetch
            _log.debug("couldn't cache the picture for %r: %s", subject, e)

    # -- lookup --------------------------------------------------------------

    def find(self, subject: str) -> Picture | None:
        subject = " ".join(str(subject or "").split())
        if not subject:
            return None
        cached = self._cached(subject)
        if cached == "none":
            return None
        if isinstance(cached, Picture):
            return cached

        deadline = _Deadline(self.timeout_seconds)
        try:
            found = self._fetch(subject, deadline, max(self.size))
            if found is None:
                self._store(subject, None)
                return None
            url, title = found
            from PIL import Image

            image = fit(Image.open(BytesIO(_get(url, deadline))), self.size)
        except Exception as e:  # noqa: BLE001 - no picture is always an acceptable answer
            # Not cached as a miss: a timeout or a dropped network says nothing about
            # whether this subject has a picture, and caching it would hide one forever.
            _log.debug("no picture for %r: %s", subject, e)
            return None
        self._store(subject, image)
        return Picture(image, subject, title, "wikipedia")
