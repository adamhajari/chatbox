from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from talkbox.config import load_settings
from talkbox.log import DailyCounter, ExchangeLog
from talkbox.policy import load_policy
from talkbox.prompt import compile_system_prompt


def _load_policy_or_exit(path: Path):
    try:
        return load_policy(path)
    except FileNotFoundError:
        sys.exit(f"Policy file not found: {path}")
    except ValidationError as e:
        sys.exit(f"Policy file {path} is invalid:\n{e}")


def cmd_chat(args, settings) -> None:
    from talkbox.pipeline import build_pipeline

    policy = _load_policy_or_exit(args.policy or settings.policy_path)
    db = args.db or settings.database_path
    counter = DailyCounter(db)
    log = ExchangeLog(db) if settings.logging_enabled else None
    pipeline = build_pipeline(policy, settings, counter, log)
    name = policy.persona.name
    g = settings.guardrails

    print(f"Talkbox ({name}) · policy {policy.version_label()} · "
          f"{pipeline.provider.name}/{pipeline.provider.model}")
    print(f"Guardrails: classifier {g.classifier.provider_settings['model']}, "
          f"output check {g.output_check.provider_settings['model'] if g.output_check.enabled else 'OFF'}")
    print(f"Logging is {'on' if log else 'off'}. Follow-ups remember this session only.")
    print("Type a question. 'new' starts a fresh session. Ctrl-D or 'quit' to exit.\n")
    try:
        while True:
            try:
                question = input("kid> ").strip()
            except EOFError:
                print()
                break
            if not question:
                continue
            if question.lower() in {"quit", "exit"}:
                break
            if question.lower() == "new":
                pipeline.history.clear()
                print("(new session)\n")
                continue
            answer = pipeline.ask(question)
            print(f"{name}> {answer.text}")
            if args.verbose:
                for s in answer.steps:
                    ms = "" if s.get("ms") is None else f" [{s['ms']} ms]"
                    print(f"   · {s['step']}: {s['decision']}{ms} {s['detail']}".rstrip())
                print(f"   · total: {answer.latency_ms} ms")
            print()
    except KeyboardInterrupt:
        print()
    finally:
        counter.close()
        if log:
            log.close()


def cmd_check_policy(args, settings) -> None:
    path = args.policy or settings.policy_path
    policy = _load_policy_or_exit(path)
    print(f"OK: {path} ({policy.version_label()})")


def cmd_show_prompt(args, settings) -> None:
    print(compile_system_prompt(_load_policy_or_exit(args.policy or settings.policy_path)))


def cmd_log(args, settings) -> None:
    if not settings.logging_enabled:
        print("Logging is off (talkbox.toml [logging] enabled = false).")
        return
    log = ExchangeLog(args.db or settings.database_path)
    for row in reversed(log.recent(args.n)):
        steps = " > ".join(f"{s['step']}:{s['decision']}" for s in json.loads(row["steps_json"]))
        print(f"[{row['ts_utc']}] {row['policy_version']} {row['model'] or '-'} {row['latency_ms']}ms")
        print(f"  Q: {row['question']}\n  A: {row['answer']}\n  {steps}\n")
    log.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="talkbox")
    parser.add_argument("--config", type=Path, default=Path("talkbox.toml"),
                        help="settings file (default: ./talkbox.toml)")
    parser.add_argument("--policy", type=Path, help="policy file (overrides the config)")
    parser.add_argument("--db", type=Path, help="SQLite log file (overrides the config)")
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="type questions as a kid would")
    chat.add_argument("-v", "--verbose", action="store_true", help="show pipeline decisions")
    chat.set_defaults(func=cmd_chat)
    sub.add_parser("check-policy", help="validate the policy file").set_defaults(func=cmd_check_policy)
    sub.add_parser("show-prompt", help="print the compiled system prompt").set_defaults(func=cmd_show_prompt)
    logp = sub.add_parser("log", help="show recent exchanges")
    logp.add_argument("-n", type=int, default=20)
    logp.set_defaults(func=cmd_log)

    args = parser.parse_args(argv)
    load_dotenv()
    if not args.config.exists():
        sys.exit(f"Config file not found: {args.config}")
    args.func(args, load_settings(args.config))


if __name__ == "__main__":
    main()
