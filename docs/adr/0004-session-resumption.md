# ADR 0004: Session Resumption and Context Window Compression

## Status

Accepted

## Context

The initial implementation had two session-lifetime problems:

1. **No context compression** — Gemini Live sessions close when the context window fills up (~15 min for continuous audio). Without compression, long conversations would crash with a 1008 error once the token budget was exceeded.

2. **No crash recovery** — Any unexpected WebSocket close (1008, 1011 internal error, network drop, server-side `goAway`) permanently killed the session. The user had to manually click "Start Session" and lost all conversation context.

Additionally, the `sessionResumptionUpdate` tokens being sent by Gemini were already stored in `localStorage`, but were **never used** when reconnecting — making them dead code.

### 1011 "Thread was cancelled" error

A related symptom was observed when users interrupted the model mid-generation on preview models (`gemini-2.5-flash-native-audio-preview-*`). This causes the server to cancel a `StartStep` thread, and in preview models this sometimes cascades into a fatal 1011 (Internal Error) that closes the WebSocket. This is a known Gemini backend instability on preview builds.

## Decision

Implement a full session resilience layer in `app.js` with four parts:

### 1. Context Window Compression

Added `contextWindowCompression` to the setup message so Gemini automatically prunes oldest turns when the context nears the limit:

```javascript
contextWindowCompression: {
    slidingWindow: {}
}
```

The `systemInstruction` (which holds rules file + directory tree) is always excluded from pruning — only turn history (user speech, AI speech, tool results) is pruned oldest-first. No manual token counting required.

### 2. Session Resumption Tokens

Enable resumption at connection time and round-trip the stored handle:

```javascript
// Always request a resumption handle from Gemini
setupMessage.setup.sessionResumption = {};

// If we have a previous handle, send it to resume the session
const storedHandle = localStorage.getItem('codiey_resumption_token');
if (storedHandle) {
    setupMessage.setup.sessionResumption.handle = storedHandle;
}
```

Incoming `sessionResumptionUpdate` tokens are stored in `localStorage('codiey_resumption_token')`. Fresh session starts (non-resumptions) clear the stale handle via `state.isResuming`.

Handles expire after ~2 hours. A stale handle causes a 1008 on reconnect; this is detected and handled by clearing the stored token and ending the session gracefully.

### 3. Graceful Reconnection (`attemptGracefulReconnect`)

Added a new function that:
1. Sets `state.reconnecting = true` (suppresses normal `endSession` logic).
2. Detaches the current WebSocket's `onclose` handler before closing (avoids double-trigger).
3. Opens a new WebSocket and calls `sendSetupMessage()` — which automatically includes the stored resumption handle.
4. Wires the new socket's handlers identically to the original.

Audio capture is **not** torn down across a reconnect (`endSession(keepAudio = true)` overload) per Gemini Live API guidance to keep the `AudioContext` alive.

### 4. `goAway` Handling

The server sends a `goAway` message ~60 seconds before forcibly closing. Previously this was in `knownTopKeys` but had no logic. Now:

```javascript
if (data.goAway) {
    // Proactively reconnect before forced close
    addSystemMessage('Reconnecting seamlessly...');
    attemptGracefulReconnect();
}
```

This makes the 60-second server cycle completely transparent to the user.

### Decision on 1011 (Interrupt crash)

The 1011 crash is a Gemini backend bug specific to preview models, not something we can fix client-side. Mitigation strategy:
- Session resumption means that even if a 1011 kills the socket, `handleWebSocketClose` will detect the unexpected close code and trigger `attemptGracefulReconnect` automatically.
- The conversation context is restored via the resumption handle.
- No explicit model downgrade was made — the preview model's quality outweighs the low-frequency 1011 risk.

## Consequences

- ✅ Sessions can run indefinitely — sliding window compression prevents context overflow.
- ✅ Network drops and server restarts auto-recover without user interaction.
- ✅ Server-initiated `goAway` closes are completely invisible to the user.
- ✅ 1011 mid-interrupt crashes auto-recover rather than terminating the session.
- ⚠️ Resumption handles expire in ~2 hours; sessions older than that cannot be resumed after a drop.
- ⚠️ `slidingWindow` prunes oldest context — very long sessions may lose early conversation details.

## What Survives Sliding Window Pruning

| Content | Storage | Survives? |
|---------|---------|-----------|
| Rules file (`.codiey/rules`) | `systemInstruction` | ✅ Always |
| Fixed instructions (persona, tool rules) | `systemInstruction` | ✅ Always |
| Directory tree | `systemInstruction` | ✅ Always |
| Tool results | Turn history | ❌ Pruned oldest-first |
| User / AI speech | Turn history | ❌ Pruned oldest-first |

## Related

- Plan: `docs/plans/PLAN-2-session-resilience.md`
- ADR: [0003](0003-audio-state-machine-tool-gate.md) — the 1008 fix that pairs with stale-handle detection
