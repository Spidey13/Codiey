# ADR 0003: Audio State Machine Fix for 1008 Tool-Call Crash

## Status

Accepted

## Context

The Gemini Multimodal Live API has a strict protocol rule: **no audio input may arrive while the model is executing a function/tool call**. Violating this causes the server to respond with WebSocket close code **1008** ("Operation is not implemented, or supported, or enabled"), immediately killing the session.

Two race condition bugs in the client-side audio state transitions were causing this:

### Bug 1 — Premature state reset on `generationComplete`

The `generationComplete` server event fires **before** the `toolCall` event in the same logical turn. The original handler reset `audioState` back to `'IDLE'` on `generationComplete`:

```javascript
// BEFORE (buggy):
if (sc.generationComplete) {
    state.audioState = 'IDLE'; // ← opens mic before toolCall arrives
}
```

This opened the mic in the gap between `generationComplete` and `toolCall`. By the time the `toolCall` arrived, audio was already flowing — causing the 1008 crash.

### Bug 2 — Weak / conditional tool detection in `modelTurn`

When a `functionCall` stub appeared inside `modelTurn.parts`, the code only conditionally entered `TOOL_PENDING`:

```javascript
// BEFORE (buggy):
if (part.functionCall) {
    if (state.audioState !== 'TOOL_PENDING') { // ← conditional could be skipped
        state.audioState = 'TOOL_PENDING';
    }
}
```

The conditional check meant that if state was already set (e.g., by a concurrent path), the transition could silently be bypassed.

### False lead — `responseModalities`

An early hypothesis was that `responseModalities: ["AUDIO"]` blocked function calling and that the fix was `["AUDIO", "TEXT"]`. This was **incorrect**:
- The Gemini Live API only supports **one** modality at a time.
- Setting `["AUDIO", "TEXT"]` causes a **1007** invalid-argument error.
- Function calling works perfectly with `["AUDIO"]` only.

## Decision

Apply two targeted fixes, leaving `responseModalities: ["AUDIO"]` unchanged:

**Fix 1 — Do not touch `audioState` on `generationComplete`:**

```javascript
if (sc.generationComplete) {
    debugLog('🏁', 'GEN', 'Generation complete');
    // Do NOT change audioState here — toolCall may be incoming
}
```

**Fix 2 — Make tool detection unconditional:**

```javascript
if (part.functionCall) {
    state.audioState = 'TOOL_PENDING'; // unconditional
    debugLog('🔧', 'TOOL_PRE', `Detected functionCall stub, entering TOOL_PENDING`);
}
```

The `'TOOL_PENDING'` state is a hard gate in the AudioWorklet message handler — no audio chunk is forwarded while in this state, regardless of VAD output:

```javascript
if (state.audioState === 'TOOL_PENDING') return;
```

`audioState` returns to `'IDLE'` only on `turnComplete`, which fires **after** tool results are sent and the model has resumed speaking.

## Consequences

- ✅ 1008 crash rate: from 100% repro → 0% across all tested tool call scenarios.
- ✅ `responseModalities: ["AUDIO"]` is preserved, keeping native audio quality.
- ✅ Debug log on `TOOL_PRE` makes state transitions fully traceable in session logs.
- ✅ Compatible with the neural VAD gate (ADR 0002) — they operate as complementary layers.

## Related

- Archive: `docs/archive/debug-notes/FINAL-ANALYSIS-1008.md`
- Archive: `docs/archive/debug-notes/CRITICAL-FINDING-responseModalities.md`
- Archive: `docs/archive/debug-notes/FIX-SUMMARY-1008.md`
- ADR: [0002](0002-neural-vad-voice-gating.md) — VAD gating that works alongside this fix
