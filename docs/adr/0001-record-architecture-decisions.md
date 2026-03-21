# ADR 0001: Record architecture decisions

## Status

Accepted

## Context

The codebase grows quickly (voice pipeline, graph UI, tools). Without written decisions, rationale is lost in chat and PRs.

## Decision

Maintain ADRs in `docs/adr/` using short, numbered markdown files. Link new ADRs from `docs/adr/README.md`.

## Consequences

- Slight overhead when making big choices; large payoff for onboarding and refactors.
- Historical fix dumps stay in `docs/archive/`; ADRs are for **durable** decisions only.
