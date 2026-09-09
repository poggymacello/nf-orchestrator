"""The Claude translator's own logic, tested with a fake client.

No live API call is made here, and none has been made anywhere: this adapter has not
been run against the real Anthropic API — no key was available in the environment it was
written in. What is tested is everything this project controls: the request it builds,
how it reads a response, and that every failure becomes one `TranslationError`.
"""

from typing import Any

import pytest

from orchestrator.llm import ClaudeTranslator, ProposedIntent
from orchestrator.translate import TranslationError, translate


class FakeMessages:
    """Stands in for `anthropic.Anthropic().messages`."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeResponse:
    def __init__(self, parsed: Any, stop_reason: str = "end_turn") -> None:
        self.parsed_output = parsed
        self.stop_reason = stop_reason


def proposal(**kwargs: Any) -> ProposedIntent:
    return ProposedIntent(**kwargs)


# --- the request this builds ---


def test_the_operator_text_is_a_user_message_never_the_system_prompt() -> None:
    """Text concatenated into the system prompt could rewrite its own instructions."""
    messages = FakeMessages(
        FakeResponse(proposal(name="edge-nf", replicas=2, environment="dev"))
    )
    ClaudeTranslator(client=messages).propose("deploy edge-nf, 2 replicas, dev")

    call = messages.calls[0]
    assert call["messages"] == [
        {"role": "user", "content": "deploy edge-nf, 2 replicas, dev"}
    ]
    assert "deploy edge-nf" not in call["system"]


def test_it_asks_for_the_loose_schema_not_the_intent() -> None:
    """If the SDK validated into Intent, the untrusted component did the validating."""
    messages = FakeMessages(FakeResponse(proposal(name="a-nf", replicas=1, environment="dev")))
    ClaudeTranslator(client=messages).propose("anything")
    assert messages.calls[0]["output_format"] is ProposedIntent


def test_it_uses_the_configured_model() -> None:
    messages = FakeMessages(FakeResponse(proposal(name="a-nf", replicas=1, environment="dev")))
    ClaudeTranslator(client=messages, model="claude-sonnet-5").propose("anything")
    assert messages.calls[0]["model"] == "claude-sonnet-5"


# --- how it reads a response ---


def test_a_good_proposal_becomes_a_plain_dict() -> None:
    messages = FakeMessages(
        FakeResponse(proposal(name="edge-nf", replicas=2, environment="dev"))
    )
    assert ClaudeTranslator(client=messages).propose("anything") == {
        "name": "edge-nf",
        "replicas": 2,
        "environment": "dev",
    }


def test_the_problem_field_never_reaches_the_caller() -> None:
    messages = FakeMessages(
        FakeResponse(proposal(name="edge-nf", replicas=2, environment="dev"))
    )
    assert "problem" not in ClaudeTranslator(client=messages).propose("anything")


def test_a_stated_problem_is_refused_not_translated() -> None:
    messages = FakeMessages(
        FakeResponse(proposal(problem="the request does not say which environment"))
    )
    with pytest.raises(TranslationError, match="could not read the request"):
        ClaudeTranslator(client=messages).propose("make it faster")


def test_a_refusal_stop_reason_is_handled() -> None:
    messages = FakeMessages(
        FakeResponse(proposal(name="a-nf", replicas=1, environment="dev"), "refusal")
    )
    with pytest.raises(TranslationError, match="declined"):
        ClaudeTranslator(client=messages).propose("anything")


def test_a_missing_parsed_output_is_refused() -> None:
    messages = FakeMessages(FakeResponse(None))
    with pytest.raises(TranslationError, match="no parsable proposal"):
        ClaudeTranslator(client=messages).propose("anything")


def test_any_sdk_error_becomes_one_translation_error() -> None:
    messages = FakeMessages(error=RuntimeError("429 rate limited"))
    with pytest.raises(TranslationError, match="translation request failed"):
        ClaudeTranslator(client=messages).propose("anything")


# --- the boundary still applies to this translator ---


def test_a_hostile_model_proposal_is_still_refused_by_the_schema() -> None:
    messages = FakeMessages(
        FakeResponse(proposal(name="edge-nf", replicas=500, environment="prod"))
    )
    with pytest.raises(TranslationError, match="failed intent validation"):
        translate("anything", ClaudeTranslator(client=messages))


def test_a_model_inventing_an_environment_is_still_refused() -> None:
    messages = FakeMessages(
        FakeResponse(proposal(name="edge-nf", replicas=1, environment="production"))
    )
    with pytest.raises(TranslationError, match="failed intent validation"):
        translate("anything", ClaudeTranslator(client=messages))


def test_a_good_model_proposal_goes_through_the_same_path() -> None:
    messages = FakeMessages(
        FakeResponse(proposal(name="edge-nf", replicas=2, environment="prod"))
    )
    result = translate("anything", ClaudeTranslator(client=messages))
    assert result["intent"] == {"name": "edge-nf", "replicas": 2, "environment": "prod"}
    assert result["translator"] == "claude"
    assert any("prod" in w for w in result["warnings"])


def test_the_text_bound_applies_before_the_model_is_called() -> None:
    """An oversized text must not be forwarded to a third party."""
    messages = FakeMessages(FakeResponse(proposal(name="a-nf", replicas=1, environment="dev")))
    with pytest.raises(TranslationError, match="over the"):
        translate("x" * 5000, ClaudeTranslator(client=messages))
    assert messages.calls == []
