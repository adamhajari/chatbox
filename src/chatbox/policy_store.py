"""The policy a running Chatbox uses, held in memory (PLAN.md D20).

The policy file is the durable copy: it's read once at startup. After that, a save
(from the parent web UI) goes through `PolicyStore.save`, which

    1. validates the edit against the schema (nothing happens if it fails),
    2. writes the policy file atomically (a crash never leaves a half-written file),
    3. swaps the in-memory policy,

so the file and the running policy always match. Each question takes one `snapshot()`
at its start and uses it throughout, so a save never changes a question in progress.
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from chatbox.policy import Policy, load_policy
from chatbox.prompt import compile_system_prompt


@dataclass(frozen=True)
class PolicySnapshot:
    policy: Policy
    system_prompt: str


class StaleEditError(Exception):
    """The edit was based on an older policy_version than the one now in use."""

    def __init__(self, base_version: int, current_version: int) -> None:
        super().__init__(f"edit is based on v{base_version}, but v{current_version} is current")
        self.base_version, self.current_version = base_version, current_version


def _snapshot(policy: Policy) -> PolicySnapshot:
    return PolicySnapshot(policy, compile_system_prompt(policy))


def _leading_comments(path: Path) -> str:
    """The comment block at the top of the file, kept when the file is rewritten."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return ""
    header = []
    for line in lines:
        if not line.startswith("#"):
            break
        header.append(line)
    return "".join(header)


def policy_to_yaml(policy: Policy) -> str:
    return yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=False, allow_unicode=True,
                          width=100)


def write_atomically(path: Path, text: str) -> None:
    """Write to a temporary file in the same directory, flush it to disk, then rename it
    over `path`. Readers see either the old file or the new one, never a partial one."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    try:  # make the rename itself durable
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


class PolicyStore:
    def __init__(self, policy: Policy, path: str | Path | None = None) -> None:
        """`path=None` keeps the policy in memory only (tests, a pipeline built from a
        Policy object)."""
        self.path = Path(path) if path is not None else None
        self._current = _snapshot(policy)
        self._lock = threading.Lock()  # one save at a time; readers never wait

    @classmethod
    def load(cls, path: str | Path) -> PolicyStore:
        return cls(load_policy(path), path)

    def snapshot(self) -> PolicySnapshot:
        # A single attribute read, so it's always a consistent (policy, prompt) pair.
        return self._current

    @property
    def policy(self) -> Policy:
        return self._current.policy

    def save(self, data: dict[str, Any], base_version: int | None = None) -> Policy:
        """Validate `data`, write it to the file, then apply it. `policy_version` is set
        to one more than the current version, whatever `data` says. If `base_version`
        is given and isn't the current version, the edit is refused (StaleEditError),
        so two parents editing at once can't silently undo each other.

        Raises pydantic.ValidationError (nothing written or applied), StaleEditError, or
        OSError from the write (nothing applied)."""
        with self._lock:
            current = self._current.policy
            if base_version is not None and base_version != current.policy_version:
                raise StaleEditError(base_version, current.policy_version)
            new = Policy.model_validate({**data, "policy_version": current.policy_version + 1})
            snap = _snapshot(new)
            if self.path is not None:
                write_atomically(self.path, _leading_comments(self.path) + policy_to_yaml(new))
            self._current = snap
            return new
