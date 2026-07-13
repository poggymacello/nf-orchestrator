# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-07-13

## Context

`nf-orchestrator` makes several non-obvious design choices (an intent contract, a lifecycle state
model, a deploy engine, an LLM guardrail). In interviews and in six months' time, the *reasoning*
behind these choices matters more than the choices themselves. Reconstructing rationale from code
and commit history is lossy.

## Decision

Record every architecture-significant decision as an Architecture Decision Record (ADR) in
`docs/design/adr/`, using the [MADR](https://adr.github.io/madr/) format. Each ADR is short
(one page or less), numbered sequentially, dated, and immutable once accepted — superseded
decisions get a new ADR that references the old one.

Each ADR captures: **Context** (the forces at play), **Decision**, **Status**, **Consequences**
(good and bad), and **Alternatives considered** (with why they were rejected).

## Consequences

- **Good:** the "why" is preserved next to the code; interview answers are traceable to a written
  record; scope decisions (including non-goals) are explicit.
- **Cost:** a small writing tax per significant decision.
- **Convention:** an ADR is required before a decision is considered final.

## Alternatives considered

- **No formal record** — rejected: rationale is lost and has to be reconstructed unreliably.
- **A single running decisions.md** — rejected: hard to reference a specific decision and to keep
  superseded reasoning without clutter.
