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
