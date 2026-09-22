"""The editor page: one server-rendered HTML form, no JavaScript. Adding or removing a
row, previewing the prompt and saving are all plain submit buttons."""

from __future__ import annotations

from html import escape
from typing import Any

from talkbox.policy import WEEKDAYS
from talkbox.web.form import KINDS

_CSS = """
:root { --fg:#1d1d1f; --muted:#6e6e73; --bg:#f5f5f7; --card:#fff; --line:#d2d2d7;
        --accent:#0a66c2; --err:#b3261e; --errbg:#fdecea; --ok:#1e6b34; --okbg:#e7f4ea; }
* { box-sizing:border-box; }
body { margin:0; font:16px/1.45 -apple-system,system-ui,sans-serif; color:var(--fg);
       background:var(--bg); padding:0 16px 96px; }
main { max-width:720px; margin:0 auto; }
h1 { font-size:1.4rem; margin:20px 0 4px; }
h2 { font-size:1.15rem; margin:0 0 4px; }
h3 { font-size:1rem; margin:16px 0 4px; }
section { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px; margin:16px 0; }
.help, .meta { color:var(--muted); font-size:.9rem; margin:2px 0 10px; }
label { display:block; font-weight:600; margin:12px 0 4px; }
label.inline { display:flex; gap:8px; align-items:center; font-weight:400; margin:8px 0; }
input[type=text], input[type=number], input[type=time], textarea, select {
  width:100%; font:inherit; padding:10px; border:1px solid var(--line); border-radius:8px;
  background:#fff; }
textarea { min-height:3.2em; resize:vertical; }
input[type=checkbox] { width:22px; height:22px; }
.row { border:1px solid var(--line); border-radius:10px; padding:4px 12px 12px; margin:10px 0; }
.days { display:flex; flex-wrap:wrap; gap:4px 14px; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
button { font:inherit; padding:10px 14px; border-radius:8px; border:1px solid var(--line);
         background:#fff; color:var(--fg); cursor:pointer; }
button.primary { background:var(--accent); color:#fff; border-color:var(--accent); font-weight:600; }
button.small { padding:6px 10px; font-size:.9rem; margin-top:10px; }
.bad input, .bad textarea, .bad select { border-color:var(--err); }
.err { color:var(--err); font-size:.9rem; margin:4px 0; }
.banner { border-radius:10px; padding:12px; margin:12px 0; }
.banner.err { background:var(--errbg); color:var(--err); font-size:1rem; }
.banner.ok { background:var(--okbg); color:var(--ok); }
.actions { position:fixed; left:0; right:0; bottom:0; background:rgba(245,245,247,.96);
           border-top:1px solid var(--line); padding:10px 16px calc(10px + env(safe-area-inset-bottom)); }
.actions div { max-width:720px; margin:0 auto; display:flex; gap:10px; justify-content:flex-end; }
pre { white-space:pre-wrap; word-wrap:break-word; font-size:.85rem; background:var(--bg);
      padding:12px; border-radius:8px; }
.hidden-default { position:absolute; left:-9999px; }
.status { display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between; }
.status form { display:inline; margin:0; }
.count { font-size:1.6rem; font-weight:700; }
.paused { background:#fff4e5; border-color:#f0b35b; }
button.warn { background:#b3261e; color:#fff; border-color:#b3261e; font-weight:600; }
"""

KIND_LABELS = {
    "allowed": "Happy to talk about",
    "blocked": "Won't talk about",
    "redirect_to_parent": "Ask a parent",
}
KIND_HELP = {
    "allowed": "Topics it answers. Simple everyday questions outside this list are fine too.",
    "blocked": "Topics it refuses; the kid hears the \"blocked topic\" reply below.",
    "redirect_to_parent": "Topics it doesn't answer itself; the kid hears that topic's reply, "
                          "word for word.",
}
CANNED_HELP = {
    "outside_schedule": ("Outside the schedule", "When it's asked outside the times above."),
    "daily_limit_reached": ("Daily limit reached", "After the day's last allowed question."),
    "blocked_topic": ("Blocked topic", "For any blocked topic, and when an answer can't be used."),
    "something_went_wrong": ("Something went wrong", "When a check or the model fails. Talkbox "
                             "never gives an unchecked answer instead."),
    "didnt_catch_that": ("Didn't catch that", "Voice only: when the recording was silent or "
                         "no words were recognized."),
}
DAY_LABELS = dict(zip(WEEKDAYS, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))


def _e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


class _Page:
    def __init__(self, data: dict[str, Any], errors: dict[str, list[str]]) -> None:
        self.data, self.errors = data, errors
        self.shown: set[str] = set()

    def err(self, key: str) -> str:
        self.shown.add(key)
        return "".join(f'<p class="err">{_e(m)}</p>' for m in self.errors.get(key, []))

    def bad(self, key: str) -> str:
        return ' class="bad"' if key in self.errors else ""

    def text(self, name: str, label: str, value: Any, help: str = "", key: str | None = None,
             area: bool = False, kind: str = "text", extra: str = "") -> str:
        key = key or name
        field = (f'<textarea id="{_e(name)}" name="{_e(name)}" rows="2">{_e(value)}</textarea>'
                 if area else
                 f'<input type="{kind}" id="{_e(name)}" name="{_e(name)}" value="{_e(value)}"{extra}>')
        help_html = f'<p class="help">{_e(help)}</p>' if help else ""
        return (f'<div{self.bad(key)}><label for="{_e(name)}">{_e(label)}</label>{help_html}'
                f'{field}{self.err(key)}</div>')

    def checkbox(self, name: str, label: str, on: bool, disabled: bool = False) -> str:
        return (f'<label class="inline"><input type="checkbox" name="{_e(name)}" value="1"'
                f'{" checked" if on else ""}{" disabled" if disabled else ""}> {_e(label)}</label>')

    # ---- sections ----

    def persona(self) -> str:
        d = self.data
        return f"""<section><h2>Who it is</h2>
{self.text("persona.name", "Name", d["persona"]["name"], "What the kids call it.")}
{self.text("persona.tone", "Tone", ", ".join(d["persona"]["tone"]),
           "A few words, separated by commas (for example: warm, playful, patient).")}
{self.text("household_age", "Age to answer for", d["household_age"],
           "Every answer is written for this age (2 to 17).", kind="number",
           extra=' inputmode="numeric" min="2" max="17"')}
</section>"""

    def answers(self) -> str:
        a = self.data["answers"]
        return f"""<section><h2>How it answers</h2>
<div class="two">
{self.text("answers.max_sentences", "Most sentences", a["max_sentences"], "1 to 10.",
           kind="number", extra=' inputmode="numeric" min="1" max="10"')}
{self.text("answers.max_words", "Most words", a["max_words"], "10 to 200.",
           kind="number", extra=' inputmode="numeric" min="10" max="200"')}
</div>
{self.checkbox("answers.say_when_unsure", "Say \"I'm not sure\" instead of guessing", a["say_when_unsure"])}
{self.checkbox("answers.ask_follow_up_question", "May end with a question back to the kid",
               a["ask_follow_up_question"])}
</section>"""

    def honesty(self) -> str:
        d = self.data["ai_disclosure"]
        return f"""<section><h2>Honesty about being a computer</h2>
{self.checkbox("_honest", "Always honest that it's a computer, not a person (always on)", True, disabled=True)}
<p class="help">This can't be turned off.</p>
{self.text("ai_disclosure.disclosure_reply", "What it says when asked if it's real",
           d["disclosure_reply"], area=True)}
</section>"""

    def topics(self) -> str:
        out = ['<section><h2>Topics</h2><p class="help">Each topic has an id (lowercase '
               'letters, numbers and underscores, used once across all lists), a name and a '
               'short description. To move a topic, change its list and save.</p>',
               self.err("topics")]
        n = 0
        for kind in KINDS:
            out.append(f'<h3>{_e(KIND_LABELS[kind])}</h3><p class="help">{_e(KIND_HELP[kind])}</p>')
            for i, t in enumerate(self.data["topics"][kind]):
                out.append(self.topic_row(n, kind, i, t))
                n += 1
            out.append(f'<button class="small" name="action" value="add_topic:{kind}">'
                       f'+ Add a topic to “{_e(KIND_LABELS[kind])}”</button>')
        out.append("</section>")
        return "\n".join(out)

    def topic_row(self, n: int, kind: str, i: int, t: dict[str, Any]) -> str:
        key = f"topics.{kind}.{i}"
        options = "".join(f'<option value="{k}"{" selected" if k == kind else ""}>'
                          f'{_e(KIND_LABELS[k])}</option>' for k in KINDS)
        reply = (self.text(f"topic.{n}.reply", "Reply (said word for word)", t.get("reply", ""),
                           key=f"{key}.reply", area=True)
                 if kind == "redirect_to_parent" else "")
        return f"""<div class="row">{self.err(key)}
{self.text(f"topic.{n}.label", "Name", t["label"], key=f"{key}.label")}
{self.text(f"topic.{n}.description", "Description", t["description"], key=f"{key}.description",
           area=True)}
{reply}
<div class="two">
{self.text(f"topic.{n}.id", "Id", t["id"], key=f"{key}.id",
           extra=' autocapitalize="none" autocorrect="off" spellcheck="false"')}
<div><label for="topic.{n}.kind">List</label>
<select id="topic.{n}.kind" name="topic.{n}.kind">{options}</select></div>
</div>
<button class="small" name="action" value="remove_topic:{n}">Remove this topic</button>
</div>"""

    def schedule(self) -> str:
        s = self.data["schedule"]
        out = [f"""<section><h2>When it's available</h2>
<p class="help">Outside these times the kid hears the "outside the schedule" reply.
Windows can't go past midnight.</p>
{self.text("schedule.timezone", "Timezone", s["timezone"], "For example America/New_York.",
           extra=' autocapitalize="none" autocorrect="off" spellcheck="false"')}
{self.err("schedule.windows")}"""]
        for n, w in enumerate(s["windows"]):
            key = f"schedule.windows.{n}"
            days = "".join(
                f'<label class="inline"><input type="checkbox" name="window.{n}.days" value="{d}"'
                f'{" checked" if d in w["days"] else ""}> {DAY_LABELS[d]}</label>' for d in WEEKDAYS)
            out.append(f"""<div class="row">{self.err(key)}
<label>Days</label><div class="days">{days}</div>{self.err(f"{key}.days")}
<div class="two">
{self.text(f"window.{n}.start", "From", w["start"], key=f"{key}.start", kind="time")}
{self.text(f"window.{n}.end", "Until", w["end"], key=f"{key}.end", kind="time")}
</div>
<button class="small" name="action" value="remove_window:{n}">Remove this window</button>
</div>""")
        out.append('<button class="small" name="action" value="add_window">+ Add a time window</button>')
        out.append(self.text("limits.daily_questions", "Questions per day",
                             self.data["limits"]["daily_questions"],
                             "Counted in the timezone above. Questions turned away don't count.",
                             kind="number", extra=' inputmode="numeric" min="0" max="1000"'))
        out.append("</section>")
        return "\n".join(out)

    def canned(self) -> str:
        c = self.data["canned_replies"]
        rows = "".join(self.text(f"canned_replies.{k}", label, c[k], help, area=True)
                       for k, (label, help) in CANNED_HELP.items())
        return f'<section><h2>Fixed replies</h2><p class="help">Said word for word.</p>{rows}</section>'


# The page's only script: keeps today's count current without reloading the page, which
# would lose unsaved edits. Without JavaScript the count still updates on reload.
_COUNT_SCRIPT = """<script>
setInterval(async () => {
  try {
    const r = await fetch("/status.json", {cache: "no-store"});
    if (r.ok) { const s = await r.json();
      document.getElementById("count").textContent = s.used + " of " + s.limit; }
  } catch (e) {}
}, 5000);
</script>"""


def exchanges_panel(exchanges: list[dict[str, Any]] | None, logging_hint: str) -> str:
    """Today's questions and answers, newest first, collapsed by default."""
    if exchanges is None:
        return (f'<section><details><summary><h2 style="display:inline">Today\'s questions and '
                f'answers</h2></summary><p class="help">{_e(logging_hint)}</p></details></section>')
    rows = "".join(
        f'<div class="row"><p class="help">{_e(x["time"])} · '
        f'{"fixed reply" if x["answered_by"] == "canned" else "model answer"}</p>'
        f'<p><strong>Q:</strong> {_e(x["question"])}</p>'
        f'<p><strong>A:</strong> {_e(x["answer"])}</p></div>'
        for x in exchanges
    ) or '<p class="help">No questions yet today.</p>'
    return (f'<section><details><summary><h2 style="display:inline">Today\'s questions and '
            f'answers ({len(exchanges)})</h2></summary><p class="help">Newest first. '
            f'Reload the page to see new ones.</p>{rows}</details></section>')


def status_panel(status: dict[str, Any]) -> str:
    """Today's count and the pause switch. Each button is its own small form, separate
    from the settings form, so pressing one never saves (or loses) settings edits."""
    paused = status["paused"]
    toggle = ('<button class="primary" name="action" value="resume">Resume</button>' if paused
              else '<button class="warn" name="action" value="pause">Pause Talkbox</button>')
    state = (f'<p><strong>Paused.</strong> Every question gets: “{_e(status["paused_reply"])}”</p>'
             if paused else "<p>Answering questions.</p>")
    return f"""<section class="{"paused" if paused else ""}"><h2>Right now</h2>
{state}
<div class="status">
<div><div class="count" id="count">{status["used"]} of {status["limit"]}</div>
<p class="help">questions asked today ({_e(status["timezone"])})</p></div>
<div style="display:flex; gap:10px; flex-wrap:wrap">
<form method="post" action="/controls"><button name="action" value="reset_count">Reset today's count</button></form>
<form method="post" action="/controls">{toggle}</form>
</div></div></section>"""


def render(
    data: dict[str, Any],
    *,
    status: dict[str, Any] | None = None,
    version_label: str,
    base_version: int,
    errors: dict[str, list[str]] | None = None,
    message: str = "",
    message_ok: bool = True,
    prompt: str,
    prompt_is_preview: bool = False,
    exchanges: list[dict[str, Any]] | None = None,
    logging_hint: str = "",
) -> str:
    errors = errors or {}
    page = _Page(data, errors)
    body = "\n".join([page.persona(), page.schedule(), page.answers(), page.honesty(),
                      page.topics(), page.canned()])
    banners = []
    if message:
        banners.append(f'<div class="banner {"ok" if message_ok else "err"}">{_e(message)}</div>')
    if errors:
        leftover = [m for k, ms in errors.items() if k not in page.shown for m in ms]
        n = sum(len(ms) for ms in errors.values())
        text = (f"Not saved: {n} thing{'s' if n != 1 else ''} to fix, shown in red below."
                if n else "")
        extra = "".join(f"<br>{_e(m)}" for m in leftover)
        banners.append(f'<div class="banner err">{_e(text)}{extra}</div>')
    prompt_title = ("Preview of the system prompt (not saved yet)" if prompt_is_preview
                    else "System prompt in use now")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Talkbox settings</title><style>{_CSS}</style></head>
<body><main>
<h1>Talkbox settings</h1>
<p class="meta">Policy {_e(version_label)}. Saved changes apply from the next question.</p>
{"".join(banners)}
{status_panel(status) if status else ""}
<form method="post" action="/">
<button class="hidden-default" name="action" value="preview" tabindex="-1" aria-hidden="true">Preview</button>
<input type="hidden" name="base_version" value="{base_version}">
{body}
<section><details{" open" if prompt_is_preview else ""}><summary><h2 style="display:inline">{_e(prompt_title)}</h2></summary>
<p class="help">What the answering model is told. Read-only; it's built from the settings above.</p>
<pre>{_e(prompt)}</pre></details></section>
{exchanges_panel(exchanges, logging_hint) if exchanges is not None or logging_hint else ""}
<div class="actions"><div>
<button name="action" value="preview">Preview prompt</button>
<button class="primary" name="action" value="save">Save</button>
</div></div>
</form>
</main>{_COUNT_SCRIPT if status else ""}</body></html>"""
