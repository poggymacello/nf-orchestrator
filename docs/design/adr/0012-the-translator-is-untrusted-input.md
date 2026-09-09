# ADR-0012: The translator is untrusted input

- **Status:** Accepted
- **Date:** 2026-09-09
- **Starts:** M6, the LLM intent layer.

## Context

M6 puts free text in front of the orchestrator: "deploy edge-nf with three replicas in
staging" should become the same JSON document a caller could have written by hand.

The tempting framing is "add an LLM". That framing decides the wrong thing first. The
interesting question is not which model translates the text — it is what the rest of the
system is allowed to assume about the translation's output, given that the output is
produced by something that can be wrong, can be manipulated by the text it was given, and
in the LLM case is a third party outside this repository.

Two properties of the existing system make this tractable, and both were already written
down. [The overview](../00-overview.md) records as a non-goal that the LLM layer is "out
of the core loop... the LLM only ever produces a JSON document that goes through the same
schema validation as any other intent". [ADR-0003](0003-intent-schema-contract.md) made
the schema strict with `extra="forbid"`. This ADR turns those from intentions into a
boundary with code on it.

## Decision

**Translating is not deploying.** `POST /intents/translate` returns a candidate intent
and nothing else happens. Putting it into the cluster is a second call the caller makes
on purpose, with the JSON in front of them. This is the strongest guardrail available and
it costs nothing: the model cannot cause a deployment because the model's output is never
on the path to one. A test asserts it, by making the deploy engine's subprocess helpers
raise if translation ever reaches them.

**The translator proposes; it does not construct.** The `Translator` protocol returns a
plain `dict`, deliberately not an `Intent`. A translator that could hand back a validated
object would be trusted to have validated it.

**Only the intent's own fields are read from a proposal.** `pick_fields` takes `name`,
`replicas` and `environment` and ignores everything else. A proposal carrying `namespace:
kube-system` or `image: attacker/payload:latest` has those dropped *before* validation
rather than rejected by it. The schema would refuse them anyway — `extra="forbid"` — but
relying on that makes the system's safety depend on a schema setting staying strict
forever. Dropping first means an added field is never even an argument.

**The candidate is validated exactly like any other intent.** Same `Intent` model, no
translator-specific path, no relaxed variant. There is no code path that reaches the
deploy engine having skipped it.

**The input is bounded, and bounded before the translator runs.** 500 characters. Checked
first, so an oversized text never reaches a translator at all — which matters most for the
LLM translator, where the text would otherwise be forwarded to a third party.

**A field that is not clearly in the text is refused, not defaulted.** `RuleTranslator`
raises when it cannot find an environment, a replica count or a name. Filling in `dev` and
`1` would turn a vague sentence into a confident, wrong deployment — and this project has
spent a milestone learning what it costs when a value that means "I do not know" is
reported as one that means something specific.

**A prod translation is flagged.** `warnings` carries a note when the candidate is for
`prod`. It does not block anything; it makes the interesting case visible in the response
the caller is about to act on.

### The default translator has no model in it

`RuleTranslator` is a deterministic keyword parser: no network, no API key, no
non-determinism. It ships as the default and is what the guardrails are tested against.

That is a real limitation and worth naming: this ADR builds the boundary an LLM plugs
into, and the rule parser is a baseline that runs everywhere, including in CI with no
credentials. The model-backed translator is the next day's work and slots in behind the
same protocol without any of the above changing.

## Verified on 2026-09-09

```
$ curl -s -X POST localhost:8000/intents/translate \
    -d '{"text":"deploy edge-nf with 3 replicas in staging"}'
{"intent":{"name":"edge-nf","replicas":3,"environment":"staging"},
 "translator":"rules","warnings":[]}

$ ... -d '{"text":"run two replicas of core-nf in prod"}'
{"intent":{"name":"core-nf","replicas":2,"environment":"prod"},"translator":"rules",
 "warnings":["text was translated into a prod intent; deploying it is a separate,
              deliberate call"]}

$ ... -d '{"text":"make it go faster"}'                                       http=422
{"detail":"no environment found in the text; expected one of dev, staging, prod"}

$ ... -d '{"text":"deploy edge-nf with 99 replicas in dev"}'                  http=422
{"detail":"candidate failed intent validation: ... replicas Input should be less than
           or equal to 10 ..."}
```

The last one is the boundary doing its job: the text was readable, the translator produced
a candidate, and the schema refused it. The translator's confidence is not authority.

The hostile cases are unit tested against a fake translator that returns whatever it is
told to — a string instead of an object, an out-of-range replica count, `environment:
production`, a name of `../../etc/passwd`, and extra `namespace` and `image` fields. Each
is refused or dropped.

## Consequences

- **Good:** no model can cause a deployment, because translation and deployment are
  different calls and only the second touches a cluster.
- **Good:** the guardrails are testable with no model at all. A fake translator returning
  hostile output exercises the boundary better than a real model would, because it can be
  made to misbehave on demand.
- **Good:** swapping in an LLM changes one object behind a protocol and nothing else.
- **Cost:** the shipped translator understands a narrow slice of English and refuses the
  rest. `make it go faster` gets a `422`, which is correct and unhelpful.
- **Cost:** `warnings` is advisory. Nothing enforces that a caller reads it before
  deploying a prod intent, and this project has no notion of approval to hang off it.
- **Cost:** dropping unknown fields silently means a translator that consistently proposes
  `namespace` is only visible through a warning nobody may look at.

## Alternatives considered

- **Translate and deploy in one call.** Rejected outright. It would put a model's output
  on the path to a cluster with no human between, and every other guardrail here would be
  compensating for that one decision.
- **Trust the schema alone and skip `pick_fields`.** Rejected: `extra="forbid"` is a
  setting, and safety that depends on a setting staying put is a a hazard the day someone
  relaxes it for an unrelated reason.
- **Let the translator return an `Intent`.** Rejected: it moves validation inside the
  untrusted component.
- **Default missing fields to `dev` and one replica.** Rejected, for the reason the M4
  drills kept producing: a value that means "unknown" must not be reported as one that
  means something specific.
- **Ship only the LLM translator and require a key.** Rejected: the entire boundary would
  then be untestable in CI, which is where it most needs to be tested.
