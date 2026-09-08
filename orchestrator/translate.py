"""Turn free text into a *candidate* intent, and treat that candidate as hostile.

The translation layer sits in front of the schema boundary and never behind it. Whatever
produces the candidate — a rule-based parser, a language model, a coin — its output is
untrusted input that goes through exactly the same `Intent` validation as a hand-written
JSON body. See ADR-0012.

Translating does not deploy. `POST /intents/translate` returns a candidate; putting it
into the cluster is a separate call the caller has to make on purpose.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from orchestrator.intent import Intent

# Free text arrives from outside and, for the LLM translator, is forwarded to a third
# party. Bounded so a caller cannot post a novel, and so the prompt stays small.
MAX_TEXT_LENGTH = 500

# Only these keys are read out of a candidate. Anything else a translator invents is
# dropped before validation rather than passed along to be argued about.
INTENT_FIELDS = ("name", "replicas", "environment")

ENVIRONMENTS = ("dev", "staging", "prod")

# Written out rather than parsed with a general number parser: the intent schema allows
# 1 to 10, so anything outside that range should fail validation, not be clamped.
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

NAME_HINT = re.compile(
    r"\b(?:called|named|name)\s+(?P<name>[a-z0-9][-a-z0-9]*)", re.IGNORECASE
)
NAME_TOKEN = re.compile(r"\b(?P<name>[a-z0-9]+(?:-[a-z0-9]+)+)\b", re.IGNORECASE)
COUNT = re.compile(r"\b(?P<count>\d{1,3})\b")


class TranslationError(ValueError):
    """The text could not be turned into a candidate intent."""


class Translator(Protocol):
    """Anything that proposes a candidate intent from text.

    Returns a plain dict, deliberately not an `Intent`: a translator is not trusted to
    construct a validated object, only to suggest field values.
    """

    name: str

    def propose(self, text: str) -> dict[str, Any]: ...


class RuleTranslator:
    """A deterministic keyword parser. No network, no key, no model.

    It is the baseline the guardrails are tested against, and it is honest about what it
    cannot do: if a field is not clearly present in the text, it raises rather than
    filling in a default. A default is how a vague sentence becomes a confident wrong
    deployment.
    """

    name = "rules"

    def propose(self, text: str) -> dict[str, Any]:
        lowered = text.lower()

        environment = next((e for e in ENVIRONMENTS if e in lowered), None)
        if environment is None:
            raise TranslationError(
                "no environment found in the text; expected one of "
                + ", ".join(ENVIRONMENTS)
            )

        replicas: int | None = None
        digits = COUNT.search(lowered)
        if digits:
            replicas = int(digits.group("count"))
        else:
            for word, value in NUMBER_WORDS.items():
                if re.search(rf"\b{word}\b", lowered):
                    replicas = value
                    break
        if replicas is None:
            raise TranslationError("no replica count found in the text")

        hint = NAME_HINT.search(text)
        if hint:
            name = hint.group("name")
        else:
            token = NAME_TOKEN.search(text)
            if not token:
                raise TranslationError(
                    "no network function name found in the text; name it explicitly, "
                    "for example 'called edge-nf'"
                )
            name = token.group("name")

        return {"name": name.lower(), "replicas": replicas, "environment": environment}


def pick_fields(candidate: Any) -> dict[str, Any]:
    """Take only the intent's own fields out of whatever the translator returned.

    A translator that returns a string, a list, or a dict carrying `namespace` or
    `image` gets those ignored here rather than at the schema, so an added field can
    never become an argument about whether the schema is strict enough.
    """
    if not isinstance(candidate, dict):
        raise TranslationError(
            f"translator returned {type(candidate).__name__}, expected an object"
        )
    return {key: candidate[key] for key in INTENT_FIELDS if key in candidate}


def warnings_for(intent: Intent, dropped: list[str]) -> list[str]:
    notes: list[str] = []
    if intent.environment == "prod":
        notes.append(
            "text was translated into a prod intent; deploying it is a separate, "
            "deliberate call"
        )
    if dropped:
        notes.append(
            "translator proposed fields that are not part of an intent and were "
            "dropped: " + ", ".join(sorted(dropped))
        )
    return notes


def translate(text: str, translator: Translator) -> dict[str, Any]:
    """Text in, validated intent out — or TranslationError, never a guess.

    The three steps are the guardrail: bound the input, take only known fields from the
    proposal, then validate exactly as any other intent is validated.
    """
    if not text or not text.strip():
        raise TranslationError("text is empty")
    if len(text) > MAX_TEXT_LENGTH:
        raise TranslationError(
            f"text is {len(text)} characters, over the {MAX_TEXT_LENGTH} limit"
        )

    proposal = translator.propose(text)
    fields = pick_fields(proposal)
    dropped = [k for k in proposal if k not in INTENT_FIELDS] if isinstance(proposal, dict) else []

    try:
        intent = Intent.model_validate(fields)
    except Exception as exc:  # pydantic ValidationError, re-raised as our own type
        raise TranslationError(f"candidate failed intent validation: {exc}") from exc

    return {
        "intent": intent.model_dump(),
        "translator": translator.name,
        "warnings": warnings_for(intent, dropped),
    }
