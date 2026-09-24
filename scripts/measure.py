#!/usr/bin/env python3
"""Measure `chatbox chat` on this machine (PLAN.md phase 6: Pi 3B vs Pi 5).

Runs the real CLI the way a kid's question goes through it, so the numbers include
interpreter start-up, imports, policy load and the live API calls.

    python scripts/measure.py                 # default questions
    python scripts/measure.py -n 5            # repeat each question 5 times
    python scripts/measure.py "Why is the sky blue?" ...

Prints per-step timings (the same ones `chatbox chat -v` shows), a summary, and peak
memory of the chatbox process. Nothing is written to disk.
"""

from __future__ import annotations

import argparse
import os
import re
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = [
    "Why is the sky blue?",
    "How do birds fly?",
    "What do octopuses eat?",
    "Why do we have to sleep?",
]
STEP = re.compile(r"·\s+(\S+?):\s+(\S+)(?:\s+\[(\d+) ms\])?")


def _child(args: list[str], stdin: str, timeout: float) -> tuple[subprocess.CompletedProcess, float]:
    """Run the CLI and report its peak resident memory in MB alongside the result."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    done = subprocess.run([sys.executable, "-m", "chatbox.cli", *args], cwd=ROOT, env=env,
                          text=True, input=stdin, capture_output=True, timeout=timeout)
    # ru_maxrss is a high-water mark across every child so far, so it is reported once at
    # the end as "the most memory a chatbox process used", not per session.
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return done, peak / 1024 if sys.platform.startswith("linux") else peak / 1024 / 1024


def startup_seconds(timeout: float) -> float:
    """Start to the first `kid>` prompt. Measured with an empty stdin: the CLI prints the
    prompt, reads end-of-input and exits, so the whole run is start-up plus a little
    teardown. (The prompt has no newline, so timing it from a pipe would really be timing
    the first answer.)"""
    started = time.monotonic()
    done, _ = _child(["chat"], "", timeout)
    elapsed = time.monotonic() - started
    if "kid>" not in done.stdout:
        sys.exit(f"`chatbox chat` didn't reach its prompt:\n{done.stdout}{done.stderr}")
    return elapsed


def run(questions: list[str], timeout: float) -> tuple[list[dict], float]:
    """One `chatbox chat -v` session asking every question in turn."""
    stdin = "".join(q + "\n" for q in questions) + "quit\n"
    done, peak_mb = _child(["chat", "-v"], stdin, timeout)

    turns: list[dict] = []
    current: dict | None = None
    for line in done.stdout.splitlines():
        if not line.lstrip().startswith("·"):
            continue
        for name, decision, ms in STEP.findall(line):
            if name == "total":
                continue
            current = current or {"steps": {}}
            current["steps"][name] = (decision, int(ms) if ms else None)
        total = re.search(r"·\s+total:\s+(\d+) ms", line)
        if total and current is not None:
            current["total_ms"] = int(total.group(1))
            current["question"] = questions[len(turns)] if len(turns) < len(questions) else "?"
            turns.append(current)
            current = None
    if not turns:
        sys.exit(f"No answers were measured:\n{done.stdout}{done.stderr}")
    return turns, peak_mb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("questions", nargs="*", default=None)
    ap.add_argument("-n", "--repeats", type=int, default=3, help="sessions to run (default 3)")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()
    questions = args.questions or QUESTIONS

    print(f"{sys.platform} · python {sys.version.split()[0]} · {len(questions)} questions "
          f"× {args.repeats} sessions\n")
    starts, all_turns, peaks = [], [], []
    for i in range(args.repeats):
        start = startup_seconds(args.timeout)
        turns, peak = run(questions, args.timeout)
        starts.append(start)
        all_turns.extend(turns)
        peaks.append(peak)
        print(f"session {i + 1}: start → first prompt {start:.2f} s")
        for t in turns:
            steps = "  ".join(f"{k} {v[1]}ms" for k, v in t["steps"].items() if v[1] is not None)
            print(f"   {t['total_ms']:>6} ms  {steps}   {t['question']}")
        print()

    totals = [t["total_ms"] for t in all_turns]
    print("summary")
    print(f"  start → first prompt : median {statistics.median(starts):.2f} s  "
          f"(min {min(starts):.2f}, max {max(starts):.2f})")
    print(f"  question end to end  : median {statistics.median(totals):.0f} ms  "
          f"(min {min(totals)}, max {max(totals)}), n={len(totals)}")
    for name in dict.fromkeys(k for t in all_turns for k in t["steps"]):
        ms = [t["steps"][name][1] for t in all_turns
              if name in t["steps"] and t["steps"][name][1] is not None]
        if ms:
            print(f"  {name:<20} : median {statistics.median(ms):.0f} ms "
                  f"(min {min(ms)}, max {max(ms)})")
    print(f"  peak memory (RSS)    : {max(peaks):.0f} MB for one chatbox process")


if __name__ == "__main__":
    main()
