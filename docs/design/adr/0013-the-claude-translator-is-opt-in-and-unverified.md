# ADR-0013: The Claude translator is opt-in, and honest about not having run

- **Status:** Accepted
- **Date:** 2026-09-09
- **Builds on:** [ADR-0012](0012-the-translator-is-untrusted-input.md), which defined the boundary
  this plugs into and changes none of.

## Context

[ADR-0012](0012-the-translator-is-untrusted-input.md) built the boundary first and shipped a
deterministic keyword parser behind it. This is the model-backed translator that was always the
point of M6.

Two facts shaped it more than any design preference. The environment it was written in has **no
Anthropic API key**, so it has never been run against the live API. And `anthropic` is a dependency
the core loop must not acquire: [the overview](../00-overview.md) records as a non-goal that the
core path "works without it and never depends on it".

## Decision

**`ClaudeTranslator` implements the same `Translator` protocol and gets no special treatment.** It
proposes a plain dict; `translate()` drops unknown fields and validates against `Intent` exactly as
it does for the rule parser. There is no LLM-specific code path to the deploy engine, because there
is no code path to the deploy engine at all — translating is still not deploying.

**The model is asked for a loose schema, deliberately not `Intent`.** `ProposedIntent` has plain
`str` / `int` fields, all optional, plus a `problem` field. Using `Intent` as the structured-output
schema would be convenient and wrong: the SDK would hand back a *validated* object, which means the
untrusted component did the validating, and the constraint would be enforced by a vendor's parser
rather than by this project. `problem` is not an intent field and is dropped by `pick_fields`
without needing to be mentioned there.

**The operator's text is a user message and never touches the system prompt.** Concatenating it
into the instruction is what makes prompt injection structurally possible; keeping it in a separate
turn means it cannot rewrite the instruction it is being read under. The system prompt says the
request is data, not instructions — which helps and is not relied upon. A test asserts the text
never appears in the system prompt.

**Every failure becomes one `TranslationError`.** Rate limits, timeouts, auth failures, connection
errors, a `refusal` stop reason, a missing `parsed_output`, and a model that reports a `problem` all
converge on the same exception, which the endpoint renders as `422`. Vendor exception classes do not
reach the API surface.

**`anthropic` is an optional extra (`pip install -e ".[llm]"`), imported lazily.** The core install,
the full test suite and every CI job run without it and without a key. A missing package produces a
`TranslationError` naming the install command rather than an `ImportError` at startup.

**Settings:** `claude-opus-5`, `effort: "low"`, `max_tokens: 1024`. This is a three-field extraction
with a fixed output shape — the cheapest setting that fits the task, not the most capable one
available.

### This adapter has not been run against the live API

No key was available, so nothing here has made a real request. What *is* tested, with a fake client
injected through the constructor, is everything this project controls: that the text goes in a user
message and not the system prompt, that the loose schema is the one requested, that a `refusal` stop
reason is handled, that a missing `parsed_output` is refused, that any SDK exception becomes one
error type, and that a hostile proposal from this translator hits the same schema wall as a hostile
proposal from any other.

What is **not** tested is the part that needs a key: whether the real SDK call signature is right,
whether the model returns usable proposals for real sentences, and how it behaves on adversarial
text. The request is written from the current API reference — `client.messages.parse(...)` with
`output_format=`, `output_config={"effort": ...}`, checking `stop_reason` for `"refusal"` — and it
is unverified. Anyone with a key can find out with one command; until then this is code written
from documentation, and the milestone map says so.

## Consequences

- **Good:** swapping the model in changed one class and no part of the boundary, which is the
  evidence that ADR-0012's protocol was the right seam.
- **Good:** CI keeps running with no key, no network to a model, and no non-determinism.
- **Good:** the loose-schema decision means the trusted `Intent` object is only ever built by this
  project, from fields it selected.
- **Cost:** the adapter is unverified against the live API. Tests that pass against a fake client
  prove the logic, not the integration, and the first real call may well fail on something the fake
  cannot model.
- **Cost:** a `problem` field asks the model to self-report ambiguity, which is exactly the kind of
  self-assessment a model is unreliable at. It is a convenience, not a control — the controls are
  the schema and the separate deploy call.
- **Cost:** the operator's text is sent to a third party. Bounded at 500 characters and never
  logged by this project, but it leaves the building, and anyone running this should know that.

## Alternatives considered

- **Use `Intent` as the structured-output schema.** Rejected: it moves validation into the
  untrusted component and makes the guarantee depend on the SDK's parser rather than on this
  project's.
- **Put the operator's text in the system prompt with the instruction.** Rejected: it is the
  standard way to make injection work.
- **Make `anthropic` a required dependency and the Claude translator the default.** Rejected: it
  would break the overview's non-goal, put a key in CI's way, and make the boundary's tests depend
  on a network call.
- **Wait for a key before writing the adapter.** Rejected in favour of writing it and labelling it
  unverified. The alternative was an empty milestone and a claim in a document; this is code that
  can be checked in one command by anyone who has a key.
- **Fake a "live verification" by testing against a recorded response.** Rejected: a cassette
  recorded from a fake is still a fake, and it would read like evidence.
