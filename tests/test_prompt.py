from talkbox.policy import Policy
from talkbox.prompt import compile_system_prompt


def test_prompt_contains_policy(policy):
    s = compile_system_prompt(policy)
    assert policy.persona.name in s
    assert "6-year-old" in s
    assert f"at most {policy.answers.max_words} words" in s
    assert policy.ai_disclosure.disclosure_reply in s
    assert policy.canned_replies.blocked_topic in s
    for t in policy.topics.allowed + policy.topics.blocked:
        assert t.label in s
    for t in policy.topics.redirect_to_parent:
        assert t.reply in s


def test_prompt_is_deterministic(policy):
    assert compile_system_prompt(policy) == compile_system_prompt(policy)


def test_prompt_follows_policy_changes(policy_data):
    policy_data["household_age"] = 9
    policy_data["persona"]["name"] = "Zed"
    policy_data["answers"]["ask_follow_up_question"] = True
    policy_data["answers"]["say_when_unsure"] = False
    s = compile_system_prompt(Policy.model_validate(policy_data))
    assert "9-year-old" in s and "You are Zed" in s
    assert "one short question" in s
    assert "I'm not sure" not in s


# ---- the picture screen (PLAN.md D28) ----------------------------------------------

def test_without_a_screen_the_prompt_says_nothing_about_one(policy):
    prompt = compile_system_prompt(policy)
    assert "screen" not in prompt.lower()


def test_with_a_screen_the_prompt_stops_it_denying_it_can_show_things(policy):
    """Without this, "can you show me an octopus?" got "I'm a computer, I can't show
    photos" -- which stopped being true the moment the panel was wired."""
    prompt = compile_system_prompt(policy, screen=True)
    assert "# The little screen" in prompt
    assert "never say you cannot show things" in prompt
    # It must not start promising or describing pictures it can't see.
    assert "Never say what the picture is" in prompt
    assert "must make complete sense with the screen switched off" in prompt
    assert "never ask the child to read anything" in prompt


def test_the_screen_section_does_not_touch_the_honesty_rules(policy):
    """Being able to show a picture doesn't make it a person."""
    without = compile_system_prompt(policy)
    with_screen = compile_system_prompt(policy, screen=True)
    assert "You are a computer program, not a person" in with_screen
    # Everything else is identical: the screen section is purely additive.
    from talkbox.prompt import SCREEN_RULES

    assert without == with_screen.replace(f"\n{SCREEN_RULES}\n", "")


def test_the_store_carries_the_screen_into_saved_policies(policy, policy_data, tmp_path):
    """A parent's edit from the settings page must recompile with the screen section,
    not lose it."""
    from talkbox.policy_store import PolicyStore

    path = tmp_path / "policy.yaml"
    store = PolicyStore(policy, path, screen=True)
    assert "# The little screen" in store.snapshot().system_prompt
    store.save({**policy_data, "household_age": 7})
    assert "# The little screen" in store.snapshot().system_prompt


def test_a_store_without_a_screen_stays_without_one(policy, policy_data, tmp_path):
    from talkbox.policy_store import PolicyStore

    store = PolicyStore(policy, tmp_path / "policy.yaml")
    store.save({**policy_data, "household_age": 7})
    assert "screen" not in store.snapshot().system_prompt.lower()
