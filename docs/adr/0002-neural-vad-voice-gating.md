# ADR 0002: Neural VAD for Voice Gating

## Status

Accepted

## Context

The original mic pipeline used a custom energy-threshold approach (`dynamicNoiseFloor` math) to decide when to send PCM audio to Gemini. This caused two distinct problems:

1. **Audio chop-offs**: The dynamic threshold wrongly silenced speech mid-utterance, causing the model to receive truncated queries and produce confused or delayed transcriptions.
2. **Tool-call crashes (1008)**: Even with threshold gating, raw PCM chunks would still leak through immediately after `generationComplete` and during the gap before a `toolCall` message arrived. Gemini's protocol treated receiving audio during tool execution as a violation and closed the WebSocket with **1008 "Operation not implemented"**.

The energy approach cannot distinguish silence from a quiet speaker or background noise reliably, and it provides no clean semantic boundary between "user is speaking" and "user has stopped".

## Decision

Replace the custom `dynamicNoiseFloor` VAD entirely with **`@ricky0123/vad-web`** (ONNX Runtime in the browser), loaded from jsDelivr CDN.

The library exposes three semantic callbacks:

| Callback | Action |
|----------|--------|
| `onSpeechStart` | `state.isUserSpeaking = true` |
| `onSpeechEnd` | `state.isUserSpeaking = false` |
| `onVADMisfire` | `state.isUserSpeaking = false` |

The AudioWorklet message handler (`workletNode.port.onmessage`) only calls `sendAudioChunk()` when **both** conditions hold:

```javascript
if (state.audioState === 'TOOL_PENDING') return; // hard block during tools
if (state.isUserSpeaking) sendAudioChunk(data);   // neural gate
```

**Tuned parameters (after latency testing):**

```javascript
positiveSpeechThreshold: 0.80,
negativeSpeechThreshold: 0.40,
minSpeechFrames:         3,
preSpeechPadFrames:     10,
redemptionFrames:       25,   // extended grace period to avoid cutting off speech
```

A fallback is included: if VAD fails to initialise (network offline, ONNX error), `state.isUserSpeaking` is set to `true` so audio can still flow, degrading gracefully to an always-open mic rather than a silent one.

## Consequences

- ✅ Transcriptions are no longer truncated — the model receives whole utterances.
- ✅ Audio never leaks into tool-execution windows; the `TOOL_PENDING` state + VAD gate are complementary defences.
- ✅ No custom threshold math to maintain.
- ⚠️ First session load downloads a ~2 MB ONNX model; a loading message is shown ("Loading VAD model...") while it initialises.
- ⚠️ VAD runs on the main thread via ONNX WASM — acceptable latency on modern hardware, but a potential concern on low-end devices.

## Related

- Archive: `docs/archive/debug-notes/FINAL-ANALYSIS-1008.md`
- Archive: `docs/archive/debug-notes/FIX-SUMMARY-1008.md`
- Plan: `docs/plans/PLAN-1-audio-pipeline.md`
- ADR: [0003](0003-audio-state-machine-tool-gate.md) — state machine fix that pairs with this decision
