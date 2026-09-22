"""The parent web UI (Phase 4): edit the policy of a running Talkbox.

It runs in the same process as `talkbox talk` / `talkbox chat` (started with `--web`),
so it shares the PolicyStore the pipeline reads from: a save applies from the next
question without a restart (PLAN.md D20).

No password yet (D7a, changed 2026-09-18): anyone on the home network can edit. Two
cheap protections stay on: requests from outside private network ranges are refused,
and a form posted from another website is refused (a browser sends that site's Origin).
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import ValidationError

from talkbox.limits import local_now
from talkbox.log import Controls, DailyCounter, ExchangeLog
from talkbox.pipeline import PAUSED_REPLY
from talkbox.policy import Policy
from talkbox.policy_store import PolicyStore, StaleEditError
from talkbox.prompt import compile_system_prompt
from talkbox.web import form as F
from talkbox.web.page import render


def _is_local_client(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def create_app(
    store: PolicyStore,
    *,
    counter: DailyCounter | None = None,
    controls: Controls | None = None,
    log: ExchangeLog | None = None,  # None = logging is off in talkbox.toml
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    lan_only: bool = True,
) -> FastAPI:
    """`counter` and `controls` are the ones the running pipeline uses; without them the
    page has no "Right now" panel."""
    app = FastAPI(title="Talkbox", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if lan_only and not _is_local_client(request.client.host if request.client else None):
            return PlainTextResponse("Only reachable from the home network.", status_code=403)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != "null":
            if urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Form posted from another site; refused.", status_code=403)
        return await call_next(request)

    def today() -> str:
        return local_now(store.policy, clock()).date().isoformat()

    def right_now() -> dict | None:
        if counter is None or controls is None:
            return None
        p = store.policy
        return {"used": counter.get(today()), "limit": p.limits.daily_questions,
                "timezone": p.schedule.timezone, "paused": controls.paused,
                "paused_reply": PAUSED_REPLY}

    def todays_exchanges() -> list[dict] | None:
        if log is None:
            return None
        tz = ZoneInfo(store.policy.schedule.timezone)
        return [{"time": datetime.fromisoformat(r["ts_utc"]).astimezone(tz).strftime("%H:%M"),
                 "question": r["question"], "answer": r["answer"],
                 "answered_by": r["answered_by"]} for r in log.for_date(today())]

    def page(data: dict, *, errors=None, message="", message_ok=True, preview: str | None = None,
             base_version: int | None = None, status: int = 200) -> HTMLResponse:
        current = store.snapshot()
        html = render(
            data,
            status=right_now(),
            exchanges=todays_exchanges(),
            logging_hint="" if log is not None else (
                "Logging is off, so questions and answers aren't saved. To keep them, set "
                "[logging] enabled = true in talkbox.toml and restart Talkbox."),
            version_label=current.policy.version_label(),
            base_version=base_version if base_version is not None else current.policy.policy_version,
            errors=errors,
            message=message,
            message_ok=message_ok,
            prompt=preview if preview is not None else current.system_prompt,
            prompt_is_preview=preview is not None,
        )
        return HTMLResponse(html, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    def show(saved: int | None = None, done: str | None = None) -> HTMLResponse:
        message = (f"Saved as version {saved}. Talkbox uses it from the next question."
                   if saved else CONTROL_MESSAGES.get(done or "", ""))
        return page(F.policy_to_data(store.policy), message=message)

    @app.get("/status.json")
    def status_json() -> Response:
        now = right_now()
        if now is None:
            return PlainTextResponse("No status.", status_code=404)
        return JSONResponse({k: now[k] for k in ("used", "limit", "paused")})

    @app.post("/controls")
    async def control(request: Request) -> Response:
        form = parse_qs((await request.body()).decode("utf-8"))
        action = form.get("action", [""])[0]
        if counter is None or controls is None or action not in CONTROL_MESSAGES:
            return PlainTextResponse("Unknown action.", status_code=400)
        if action == "reset_count":
            counter.reset(today())
        else:
            controls.set_paused(action == "pause")
        return RedirectResponse(f"/?done={action}", status_code=303)

    @app.post("/", response_class=HTMLResponse)
    async def submit(request: Request) -> Response:
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        action = form.get("action", ["preview"])[0]
        try:
            base_version = int(form.get("base_version", [""])[0])
        except ValueError:
            base_version = None

        def row(prefix: str) -> int | None:
            if not action.startswith(prefix):
                return None
            try:
                return int(action[len(prefix):])
            except ValueError:
                return None

        data = F.parse_form(form, row("remove_topic:"), row("remove_window:"))

        if action.startswith("add_topic:"):
            kind = action.split(":", 1)[1]
            if kind in F.KINDS:
                data["topics"][kind].append(F.blank_topic(kind))
        elif action == "add_window":
            data["schedule"]["windows"].append(F.blank_window())

        if action == "save":
            try:
                saved = store.save(data, base_version)
            except ValidationError as e:
                return page(data, errors=F.errors_by_field(e), base_version=base_version,
                            status=422)
            except StaleEditError as e:
                return page(data, message=(
                    f"Not saved: someone else saved version {e.current_version} while you "
                    f"were editing. Your changes are still below. Save again to replace their "
                    f"version with yours, or reload the page to start from theirs."),
                    message_ok=False, status=409)
            except OSError as e:
                return page(data, message=f"Not saved: couldn't write the policy file ({e}). "
                            "Talkbox is still using the previous version.",
                            message_ok=False, base_version=base_version, status=500)
            return RedirectResponse(f"/?saved={saved.policy_version}", status_code=303)

        unsaved = "Not saved yet. Press Save to apply."
        if action == "preview":
            try:
                preview_policy = Policy.model_validate(
                    {**data, "policy_version": store.policy.policy_version + 1})
            except ValidationError as e:
                return page(data, errors=F.errors_by_field(e), base_version=base_version,
                            message="Fix these to see the preview.", message_ok=False)
            return page(data, preview=compile_system_prompt(preview_policy),
                        base_version=base_version, message=unsaved)
        return page(data, base_version=base_version, message=unsaved)

    return app


CONTROL_MESSAGES = {
    "pause": "Paused. Every question now gets the resting reply until you press Resume.",
    "resume": "Resumed. Talkbox answers from the next question.",
    "reset_count": "Today's question count is back to 0.",
}


# ---- running it next to Talkbox -------------------------------------------------

def lan_address() -> str | None:
    """This computer's address on the home network (the one its default route uses).
    No packets are sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # TEST-NET-1: never routed anywhere
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ip if ipaddress.ip_address(ip).is_private else None


def resolve_host(host: str) -> str:
    if host != "auto":
        return host
    ip = lan_address()
    if ip is None:
        raise RuntimeError("couldn't find a home-network address; set [web] host in talkbox.toml")
    return ip


class WebServer:
    """uvicorn in a background thread of the Talkbox process."""

    def __init__(self, store: PolicyStore, host: str, port: int, *,
                 counter: DailyCounter | None = None, controls: Controls | None = None,
                 log: ExchangeLog | None = None) -> None:
        import uvicorn

        self.host = resolve_host(host)
        self.port = port
        config = uvicorn.Config(create_app(store, counter=counter, controls=controls, log=log), host=self.host, port=port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="talkbox-web", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self, timeout_s: float = 5.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"the parent web UI couldn't start on {self.host}:{self.port} "
                                   "(is the port in use? change [web] port in talkbox.toml)")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=3)
