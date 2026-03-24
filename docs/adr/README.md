# Architecture Decision Records (ADR)

ADRs capture **why** a significant technical choice was made, so future you (and contributors) don’t re-litigate the same decisions.

## Format

Use one file per decision: `NNNN-short-title.md` (e.g. `0002-use-gemini-live-native-audio.md`).

Suggested sections:

1. **Status** — Proposed / Accepted / Superseded
2. **Context** — Problem or forces at play
3. **Decision** — What we chose
4. **Consequences** — Tradeoffs, follow-ups

## Index

| ADR | Title | Status |
|-----|--------|--------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-neural-vad-voice-gating.md) | Neural VAD for voice gating (`@ricky0123/vad-web`) | Accepted |
| [0003](0003-audio-state-machine-tool-gate.md) | Audio state machine fix for 1008 tool-call crash | Accepted |
| [0004](0004-session-resumption.md) | Session resumption and context window compression | Accepted |
