from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from talkbox.config import load_settings
from talkbox.log import Controls, DailyCounter, ExchangeLog
from talkbox.policy import load_policy
from talkbox.policy_store import PolicyStore
from talkbox.prompt import compile_system_prompt


def _load_policy_or_exit(path: Path):
    try:
        return load_policy(path)
    except FileNotFoundError:
        sys.exit(f"Policy file not found: {path}")
    except ValidationError as e:
        sys.exit(f"Policy file {path} is invalid:\n{e}")


def _start_web(args, settings, store: PolicyStore, counter, controls, log):
    """With --web, serve the parent settings page from this process, sharing `store`."""
    if not args.web:
        return None
    from talkbox.web.app import WebServer

    try:
        server = WebServer(store, settings.web.host, settings.web.port,
                           counter=counter, controls=controls, log=log)
        server.start()
    except RuntimeError as e:
        sys.exit(f"Parent web UI: {e}")
    print(f"Parent settings: open {server.url} on a phone or computer on this network.")
    print("  No password yet: anyone on the home network can change the settings.")
    return server


def _open_db(args, settings):
    db = args.db or settings.database_path
    counter, controls = DailyCounter(db), Controls(db)
    log = ExchangeLog(db) if settings.logging_enabled else None
    if controls.paused:
        print("Talkbox is PAUSED (from the settings page): every question gets the resting reply.")
    return counter, controls, log


def cmd_chat(args, settings) -> None:
    from talkbox.pipeline import build_pipeline

    path = args.policy or settings.policy_path
    store = PolicyStore(_load_policy_or_exit(path), path)
    policy = store.policy
    counter, controls, log = _open_db(args, settings)
    pipeline = build_pipeline(store, settings, counter, log, controls)
    g = settings.guardrails
    web = _start_web(args, settings, store, counter, controls, log)

    print(f"Talkbox ({policy.persona.name}) · policy {policy.version_label()} · "
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
            print(f"{pipeline.policy.persona.name}> {answer.text}")
            if args.verbose:
                _print_steps(answer.steps, answer.latency_ms)
            print()
    except KeyboardInterrupt:
        print()
    finally:
        if web:
            web.stop()
        counter.close()
        controls.close()
        if log:
            log.close()


def _print_steps(steps: list[dict], total_ms: int | None = None) -> None:
    for s in steps:
        ms = "" if s.get("ms") is None else f" [{s['ms']} ms]"
        print(f"   · {s['step']}: {s['decision']}{ms} {s['detail']}".rstrip())
    if total_ms is not None:
        print(f"   · total: {total_ms} ms")


def cmd_talk(args, settings) -> None:
    from dataclasses import fields

    from talkbox.audio import audio_device_problem
    from talkbox.audio.laptop import (Keyboard, KeyboardPress, LaptopSpeaker, PushToTalkMic,
                                      PushToTalkSettings)
    from talkbox.audio.press import AnyPress
    from talkbox.pipeline import build_pipeline
    from talkbox.speech import make_stt, make_tts
    from talkbox.voice import VoiceSettings, VoiceTurn

    if not sys.stdin.isatty():
        sys.exit("talkbox talk needs an interactive terminal (it listens for the spacebar)")
    problem = audio_device_problem()
    if problem:
        sys.exit(f"talkbox talk needs a microphone and a speaker, but {problem}\n"
                 "`talkbox chat` works without them.")
    if settings.speech is None:
        sys.exit("talkbox.toml has no [speech] section (see README: voice setup)")
    sp = settings.speech
    path = args.policy or settings.policy_path
    store = PolicyStore(_load_policy_or_exit(path), path)
    policy = store.policy
    counter, controls, log = _open_db(args, settings)
    pipeline = build_pipeline(store, settings, counter, log, controls)

    def pick(cls):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in sp.voice.items() if k in names})

    ptt_settings, voice_settings = pick(PushToTalkSettings), pick(VoiceSettings)
    stt, tts = make_stt(sp.stt_name, sp.stt_settings), make_tts(sp.tts_name, sp.tts_settings)
    speaker = LaptopSpeaker(ptt_settings.output_device, tts.sample_rate)
    turn = VoiceTurn(pipeline, stt, tts, speaker, voice_settings)
    g = settings.guardrails
    web = _start_web(args, settings, store, counter, controls, log)
    print(f"Talkbox ({policy.persona.name}) · policy {policy.version_label()} · "
          f"{pipeline.provider.name}/{pipeline.provider.model}")
    print(f"Speech: {stt.name}/{stt.model} → text → {tts.name}/{tts.voice}. "
          f"Output check {'on' if g.output_check.enabled else 'OFF'}. "
          f"Logging is {'on' if log else 'off'}; no audio is stored.")
    print("Let go to send. 'n' starts a fresh session, 'q' quits.")
    try:
        with Keyboard() as kb:
            sources = [KeyboardPress(kb, ptt_settings.key_repeat_wait_seconds,
                                     ptt_settings.release_gap_seconds)]
            if ptt_settings.button_gpio is not None:
                from talkbox.audio.pi import ButtonPress

                try:
                    sources.append(ButtonPress(ptt_settings.button_gpio))
                except RuntimeError as e:
                    sys.exit(f"Button: {e}")
            press = AnyPress(sources)
            mic = PushToTalkMic(press, speaker, ptt_settings)
            print(f"Hold {press.name} and talk.\n")
            while True:
                command = press.poll_command(None)
                if command == "quit":
                    break
                if command == "new":
                    pipeline.history.clear()
                    print("(new session)\n")
                    continue
                print("listening…", end="", flush=True)
                result = turn.run(mic.record(), ptt_settings.sample_rate)
                print("\r", end="")
                if result.transcript is None and result.spoken is None:
                    print("(tap too short; hold the spacebar while talking)")
                else:
                    print(f"kid> {result.transcript or '(nothing recognized)'}")
                    print(f"{pipeline.policy.persona.name}> {result.spoken}")
                if args.verbose:
                    _print_steps(result.steps)
                print()
                press.flush()
    except KeyboardInterrupt:
        print()
    finally:
        if web:
            web.stop()
        counter.close()
        controls.close()
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
    chat.add_argument("--web", action="store_true", help="also serve the parent settings page")
    chat.set_defaults(func=cmd_chat)
    talk = sub.add_parser("talk", help="hold the spacebar and ask out loud")
    talk.add_argument("-v", "--verbose", action="store_true", help="show pipeline decisions")
    talk.add_argument("--web", action="store_true", help="also serve the parent settings page")
    talk.set_defaults(func=cmd_talk)
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
