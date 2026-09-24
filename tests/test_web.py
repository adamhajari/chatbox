import re

import pytest
from fastapi.testclient import TestClient

from chatbox.policy import WEEKDAYS, Policy
from chatbox.policy_store import PolicyStore
from chatbox.web.app import _is_local_client, create_app
from chatbox.web.form import KINDS, parse_form, policy_to_data
from tests.conftest import TEST_POLICY


@pytest.fixture
def policy_file(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(TEST_POLICY.read_text())
    return path


@pytest.fixture
def store(policy_file):
    return PolicyStore.load(policy_file)


@pytest.fixture
def client(store):
    return TestClient(create_app(store, lan_only=False))


def form_for(store) -> dict[str, list[str]]:
    """The form a browser would send for the current policy, unchanged."""
    d = policy_to_data(store.policy)
    f = {
        "base_version": [str(d["policy_version"])],
        "household_age": [str(d["household_age"])],
        "persona.name": [d["persona"]["name"]],
        "persona.tone": [", ".join(d["persona"]["tone"])],
        "answers.max_sentences": [str(d["answers"]["max_sentences"])],
        "answers.max_words": [str(d["answers"]["max_words"])],
        "ai_disclosure.disclosure_reply": [d["ai_disclosure"]["disclosure_reply"]],
        "schedule.timezone": [d["schedule"]["timezone"]],
        "limits.daily_questions": [str(d["limits"]["daily_questions"])],
    }
    for k in ("say_when_unsure", "ask_follow_up_question"):
        if d["answers"][k]:
            f[f"answers.{k}"] = ["1"]
    n = 0
    for kind in KINDS:
        for t in d["topics"][kind]:
            f[f"topic.{n}.kind"] = [kind]
            for field, value in t.items():
                f[f"topic.{n}.{field}"] = [value]
            n += 1
    for i, w in enumerate(d["schedule"]["windows"]):
        f[f"window.{i}.days"] = list(w["days"])
        f[f"window.{i}.start"], f[f"window.{i}.end"] = [w["start"]], [w["end"]]
    for k, v in d["canned_replies"].items():
        f[f"canned_replies.{k}"] = [v]
    return f


def topic_row(form, topic_id) -> int:
    return next(int(k.split(".")[1]) for k, v in form.items()
                if k.endswith(".id") and k.startswith("topic.") and v == [topic_id])


def post(client, form, action):
    return client.post("/", data={**form, "action": action}, follow_redirects=False)


def test_unchanged_form_round_trips(store):
    data = parse_form(form_for(store))
    assert Policy.model_validate({**data, "policy_version": store.policy.policy_version}) == store.policy


def test_page_shows_everything(client, store):
    html = client.get("/").text
    p = store.policy
    assert f"Policy {p.version_label()}" in html
    for text in [p.persona.name, p.canned_replies.didnt_catch_that, p.schedule.timezone,
                 p.topics.redirect_to_parent[0].reply, "System prompt in use now"]:
        assert text.replace("'", "&#x27;") in html
    assert re.search(r'type="checkbox" name="_honest" value="1" checked disabled', html)


def test_save_blocks_a_topic_and_applies_live(client, store, policy_file):
    form = form_for(store)
    n = topic_row(form, store.policy.topics.allowed[0].id)
    form[f"topic.{n}.kind"] = ["blocked"]
    old_version = store.policy.policy_version

    r = post(client, form, "save")
    assert r.status_code == 303
    assert store.policy.policy_version == old_version + 1
    blocked = [t.id for t in store.policy.topics.blocked]
    assert form[f"topic.{n}.id"][0] in blocked
    assert f"version {old_version + 1}" in client.get(r.headers["location"]).text
    assert policy_file.read_text() and PolicyStore.load(policy_file).policy == store.policy


def test_change_redirect_reply(client, store):
    form = form_for(store)
    n = topic_row(form, store.policy.topics.redirect_to_parent[0].id)
    form[f"topic.{n}.reply"] = ["Let's ask a grown-up together."]
    assert post(client, form, "save").status_code == 303
    assert store.policy.topics.redirect_to_parent[0].reply == "Let's ask a grown-up together."
    assert "Let's ask a grown-up together." in store.snapshot().system_prompt


@pytest.mark.parametrize("edit, key, message", [
    (lambda f, s: f.update({f"topic.{topic_row(f, s.policy.topics.redirect_to_parent[0].id)}.reply": [""]}),
     "reply", "This can&#x27;t be empty."),
    (lambda f, s: f.update({"window.0.start": ["18:00"], "window.0.end": ["09:00"]}),
     "window", "The end time must be after the start time"),
    (lambda f, s: f.update({f"topic.{topic_row(f, s.policy.topics.blocked[0].id)}.id":
                            [s.policy.topics.allowed[0].id]}),
     "topics", "Each topic needs its own id; used more than once"),
    (lambda f, s: f.update({"limits.daily_questions": ["lots"]}), "limit", "Enter a whole number."),
    (lambda f, s: f.update({"canned_replies.didnt_catch_that": ["   "]}), "canned",
     "This can&#x27;t be empty."),
    (lambda f, s: f.update({"window.0.days": []}), "days", "Pick at least one day."),
])
def test_bad_edit_is_rejected_with_a_clear_message(client, store, policy_file, edit, key, message):
    form = form_for(store)
    edit(form, store)
    before_file, before = policy_file.read_bytes(), store.snapshot()

    r = post(client, form, "save")
    assert r.status_code == 422
    assert message in r.text and "Not saved" in r.text
    assert policy_file.read_bytes() == before_file and store.snapshot() is before


def test_error_appears_next_to_its_field(client, store):
    form = form_for(store)
    n = topic_row(form, store.policy.topics.redirect_to_parent[0].id)
    form[f"topic.{n}.reply"] = [""]
    html = post(client, form, "save").text
    field = html.index(f'name="topic.{n}.reply"')
    assert html.index("This can&#x27;t be empty.", field) - field < 200


def test_disclosure_cannot_be_switched_off(client, store):
    form = form_for(store)
    form["ai_disclosure.always_honest_about_being_a_computer"] = [""]
    assert post(client, form, "save").status_code == 303
    assert store.policy.ai_disclosure.always_honest_about_being_a_computer is True


def test_add_and_remove_rows_without_saving(client, store, policy_file):
    before_file = policy_file.read_bytes()
    form = form_for(store)
    n_topics = sum(len(store.policy.topics.model_dump()[k]) for k in KINDS)

    html = post(client, form, "add_topic:redirect_to_parent").text
    assert f'name="topic.{n_topics}.reply"' in html and "Not saved yet" in html
    html = post(client, form, "remove_topic:0").text
    assert store.policy.topics.allowed[0].label not in html.split("<h3>")[1]
    html = post(client, form, "add_window").text
    assert f'name="window.{len(store.policy.schedule.windows)}.start"' in html
    assert policy_file.read_bytes() == before_file


def test_add_topic_then_save(client, store):
    form = form_for(store)
    n = sum(len(store.policy.topics.model_dump()[k]) for k in KINDS)
    form.update({f"topic.{n}.kind": ["blocked"], f"topic.{n}.id": ["slime"],
                 f"topic.{n}.label": ["Slime"], f"topic.{n}.description": [""]})
    assert post(client, form, "save").status_code == 303
    assert store.policy.topics.blocked[-1].id == "slime"


def test_remove_window_then_save(client, store):
    form = form_for(store)
    windows = len(store.policy.schedule.windows)
    assert windows >= 2
    html = post(client, form, "remove_window:0").text  # the server drops the row
    assert f'name="window.{windows - 1}.start"' not in html
    del form["window.0.days"], form["window.0.start"], form["window.0.end"]
    assert post(client, form, "save").status_code == 303
    assert len(store.policy.schedule.windows) == windows - 1


def test_preview_shows_unsaved_prompt(client, store, policy_file):
    before_file = policy_file.read_bytes()
    form = form_for(store)
    form["persona.name"] = ["Zed"]
    html = post(client, form, "preview").text
    assert "not saved yet" in html and "You are Zed," in html
    assert store.policy.persona.name != "Zed" and policy_file.read_bytes() == before_file


def test_stale_edit_is_refused(client, store, policy_file):
    form = form_for(store)
    assert post(client, form, "save").status_code == 303  # someone else saves first
    form["persona.name"] = ["Zed"]
    r = post(client, form, "save")
    assert r.status_code == 409 and "someone else saved" in r.text
    assert store.policy.persona.name != "Zed"


def test_cross_site_post_is_refused(client, store):
    r = client.post("/", data={**form_for(store), "action": "save"},
                    headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_only_home_network_clients(store):
    assert TestClient(create_app(store)).get("/").status_code == 403  # "testclient" isn't an IP
    for ok in ["192.168.1.20", "10.0.0.5", "172.16.3.4", "127.0.0.1", "::1", "fe80::1",
               "::ffff:192.168.1.9"]:
        assert _is_local_client(ok), ok
    for bad in ["8.8.8.8", "2001:4860::8888", None, "testclient"]:
        assert not _is_local_client(bad), bad


def test_days_keep_week_order(store):
    form = form_for(store)
    form["window.0.days"] = ["sun", "mon"]
    assert parse_form(form)["schedule"]["windows"][0]["days"] == ["mon", "sun"]
    assert WEEKDAYS[0] == "mon"


# ---- "Right now": today's count, reset, pause -----------------------------------

from chatbox.log import Controls, DailyCounter  # noqa: E402
from chatbox.pipeline import PAUSED_REPLY, Pipeline  # noqa: E402
from tests.conftest import WED_NOON, AllowAll, FakeProvider, PassAll  # noqa: E402

TODAY = WED_NOON.date().isoformat()


@pytest.fixture
def db(tmp_path):
    counter, controls = DailyCounter(tmp_path / "t.db"), Controls(tmp_path / "t.db")
    yield counter, controls
    counter.close(), controls.close()


@pytest.fixture
def live(store, db):
    counter, controls = db
    client = TestClient(create_app(store, counter=counter, controls=controls,
                                   clock=lambda: WED_NOON, lan_only=False))
    pipeline = Pipeline(store, FakeProvider(), counter, classifier=AllowAll(),
                        output_checker=PassAll(), clock=lambda: WED_NOON, controls=controls)
    return client, pipeline, counter, controls


def test_page_shows_todays_count(live, store):
    client, pipeline, *_ = live
    pipeline.ask("one"), pipeline.ask("two")
    assert f"2 of {store.policy.limits.daily_questions}" in client.get("/").text


def test_reset_count(live, store):
    client, pipeline, counter, _ = live
    pipeline.ask("one")
    r = client.post("/controls", data={"action": "reset_count"}, follow_redirects=False)
    assert r.status_code == 303 and counter.get(TODAY) == 0
    assert f"0 of {store.policy.limits.daily_questions}" in client.get(r.headers["location"]).text


def test_reset_lets_questions_through_after_the_limit(live, store):
    client, pipeline, *_ = live
    store.save({**policy_to_data(store.policy), "limits": {"daily_questions": 1}})
    pipeline.ask("one")
    assert pipeline.ask("two").text == store.policy.canned_replies.daily_limit_reached
    client.post("/controls", data={"action": "reset_count"})
    assert pipeline.ask("three").answered_by == "model"


def test_pause_answers_everything_with_resting_reply(live):
    client, pipeline, counter, controls = live
    r = client.post("/controls", data={"action": "pause"}, follow_redirects=False)
    assert r.status_code == 303 and controls.paused
    assert "Resume" in client.get("/").text

    answer = pipeline.ask("why is the sky blue?")
    assert answer.text == "I'm currently resting. Let's talk later." == PAUSED_REPLY
    assert answer.steps[0]["step"] == "pause" and pipeline.provider.calls == []
    assert counter.get(TODAY) == 0  # paused questions don't count

    client.post("/controls", data={"action": "resume"})
    assert not controls.paused and pipeline.ask("why?").answered_by == "model"


def test_pause_survives_a_restart(tmp_path):
    c = Controls(tmp_path / "t.db")
    c.set_paused(True)
    c.close()
    assert Controls(tmp_path / "t.db").paused


def test_controls_ignore_unknown_actions_and_other_sites(live):
    client, *_ = live
    assert client.post("/controls", data={"action": "explode"}).status_code == 400
    assert client.post("/controls", data={"action": "pause"},
                       headers={"origin": "http://evil.example"}).status_code == 403


def test_control_buttons_do_not_touch_unsaved_settings(live, store, policy_file):
    client, *_ = live
    before = policy_file.read_bytes()
    client.post("/controls", data={"action": "pause"})
    assert policy_file.read_bytes() == before


def test_status_json_follows_questions(live, store):
    client, pipeline, *_ = live
    assert client.get("/status.json").json()["used"] == 0
    pipeline.ask("one")
    assert client.get("/status.json").json() == {
        "used": 1, "limit": store.policy.limits.daily_questions, "paused": False,
        "volume": 100}
    assert 'id="count"' in client.get("/").text and "/status.json" in client.get("/").text


# ---- today's questions and answers ------------------------------------------------

from chatbox.log import ExchangeLog  # noqa: E402


def test_todays_exchanges_are_listed_newest_first(store, db, tmp_path):
    counter, controls = db
    log = ExchangeLog(tmp_path / "t.db")
    pipeline = Pipeline(store, FakeProvider("Because <sunlight> scatters."), counter, log,
                        classifier=AllowAll(), output_checker=PassAll(), clock=lambda: WED_NOON)
    client = TestClient(create_app(store, counter=counter, controls=controls, log=log,
                                   clock=lambda: WED_NOON, lan_only=False))
    pipeline.ask("why is the sky blue?")
    pipeline.ask("why is grass green?")
    html = client.get("/").text
    section = html[html.index("Today's questions and answers (2)"):]
    assert section.index("grass green") < section.index("sky blue")
    assert "12:00 · model answer" in section
    assert "&lt;sunlight&gt;" in section and "<sunlight>" not in section  # escaped
    log.close()


def test_other_days_are_not_listed(store, db, tmp_path):
    counter, controls = db
    log = ExchangeLog(tmp_path / "t.db")
    Pipeline(store, FakeProvider(), counter, log, classifier=AllowAll(), output_checker=PassAll(),
             clock=lambda: WED_NOON).ask("yesterday's question")
    tomorrow = WED_NOON.replace(day=WED_NOON.day + 1)
    client = TestClient(create_app(store, counter=counter, controls=controls, log=log,
                                   clock=lambda: tomorrow, lan_only=False))
    html = client.get("/").text
    assert "yesterday&#x27;s question" not in html and "No questions yet today." in html
    log.close()


def test_logging_off_says_so(live):
    client, pipeline, *_ = live
    pipeline.ask("secret question")
    html = client.get("/").text
    assert "Logging is off" in html and "secret question" not in html


# ---- volume (device state, not policy) ---------------------------------------------

def test_volume_defaults_to_full_and_saves(live):
    client, _, _, controls = live
    assert controls.volume == 100

    r = client.post("/controls", data={"action": "set_volume", "volume": "40"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert controls.volume == 40
    assert "Volume saved" in r.text
    assert 'value="40"' in r.text          # the slider comes back where it was left


def test_volume_is_clamped_not_rejected(live):
    """A slider can't send these, but a hand-made request can."""
    client, _, _, controls = live
    client.post("/controls", data={"action": "set_volume", "volume": "500"})
    assert controls.volume == 100
    client.post("/controls", data={"action": "set_volume", "volume": "-20"})
    assert controls.volume == 0


def test_volume_that_is_not_a_number_is_refused(live):
    client, _, _, controls = live
    r = client.post("/controls", data={"action": "set_volume", "volume": "loud"})
    assert r.status_code == 400
    assert controls.volume == 100          # unchanged


def test_volume_survives_a_restart(tmp_path):
    """It lives in the database, like the pause switch, so a reboot doesn't undo it."""
    from chatbox.log import Controls

    db = tmp_path / "t.db"
    controls = Controls(db)
    controls.set_volume(35)
    controls.close()

    again = Controls(db)
    assert again.volume == 35
    again.close()
