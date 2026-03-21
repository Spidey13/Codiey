# Plan 1: Audio Pipeline & Three-State Session Manager

> Priority: HIGHEST — fixes tool call race conditions and stops burning context on silence
> Files: `codiey/static/app.js`, `codiey/static/pcm-processor.js`

---

## Current State

- `pcm-processor.js` (AudioWorklet) captures mic input, converts Float32 → Int16 PCM, posts every buffer to main thread unconditionally
- `app.js` forwards every PCM chunk to Gemini via WebSocket regardless of whether anyone is speaking
- No energy detection, no gating, no awareness of tool call state
- Burns ~25 tokens/second on silence between turns
- Race condition: tool response and user speech can arrive at the model simultaneously, causing confused output
- `generationComplete` from Gemini falls through to UNKNOWN_MSG logging

---

## Phase 1: Add Energy Detection to AudioWorklet

**File:** `codiey/static/pcm-processor.js`

The worklet currently converts Float32 to Int16 PCM and posts to main thread. Add RMS energy computation so the main thread can make gating decisions.

**In the `process()` method, before converting to Int16**, compute RMS energy from the Float32 buffer:

```javascript
// Compute RMS energy from the raw Float32 samples
let sumSquares = 0;
for (let j = 0; j < this.bufferSize; j++) {
    sumSquares += this.buffer[j] * this.buffer[j];
}
const energy = Math.sqrt(sumSquares / this.bufferSize);
```

**Change the postMessage call** (currently line 34) from:

```javascript
this.port.postMessage({
    type: 'pcm',
    data: pcmData.buffer
}, [pcmData.buffer]);
```

To:

```javascript
this.port.postMessage({
    type: 'pcm',
    data: pcmData.buffer,
    energy: energy
}, [pcmData.buffer]);
```

No other changes to the worklet. It always captures and always posts. The main thread decides what to do with the data.

---

## Phase 2: Three-State Session Manager in app.js

**File:** `codiey/static/app.js`

### New State Properties

Add to the `state` object (around line 26):

```javascript
audioState: 'IDLE',  // 'STREAMING' | 'IDLE' | 'TOOL_PENDING'
audioBuffer: [],     // Buffered PCM chunks during TOOL_PENDING
```

Add a constant near the top of the file:

```javascript
const ENERGY_THRESHOLD = 0.015; // RMS threshold for voice activity detection
```

### State Machine Transitions

```
IDLE ──(energy > threshold)──────────────> STREAMING
STREAMING ──(turnComplete)───────────────> IDLE
STREAMING ──(Tier 1 toolCall received)───> TOOL_PENDING
IDLE ──(modelTurn audio received)────────> STREAMING  (user can barge-in)

TOOL_PENDING ──(tool result, no speech)──> send response to Gemini ──> STREAMING
TOOL_PENDING ──(energy > threshold)──────> flush buffer to Gemini,
                                           skip tool response,
                                           start 3s timeout fallback ──> STREAMING
```

### Extract Audio Send Helper

Extract the inline audio sending code from `startAudioCapture()` (lines 500-517) into a reusable function:

```javascript
function sendAudioChunk(pcmArrayBuffer) {
    const base64 = arrayBufferToBase64(pcmArrayBuffer);
    state.ws.send(JSON.stringify({
        realtimeInput: {
            audio: {
                data: base64,
                mimeType: "audio/pcm;rate=16000"
            }
        }
    }));
    state.audioChunksSent++;
    if (state.audioChunksSent % 50 === 1) {
        debugLog('mic', 'AUDIO_IN', `Sent mic chunk #${state.audioChunksSent} (${base64.length} chars b64)`);
    }
}
```

### Rewrite the Worklet Message Handler

**Replace** the current `workletNode.port.onmessage` handler (lines 499-518) with:

```javascript
workletNode.port.onmessage = (event) => {
    if (event.data.type !== "pcm" || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;

    const { data, energy } = event.data;

    switch (state.audioState) {
        case 'STREAMING':
            sendAudioChunk(data);
            break;

        case 'IDLE':
            // Silent gating: only resume when voice detected
            if (energy > ENERGY_THRESHOLD) {
                state.audioState = 'STREAMING';
                sendAudioChunk(data);
            }
            // Otherwise: discard silent chunk (saves ~25 tokens/sec)
            break;

        case 'TOOL_PENDING':
            // Buffer locally, monitor for user speech
            state.audioBuffer.push(data);
            if (energy > ENERGY_THRESHOLD) {
                // User is speaking during tool call — flush buffer to Gemini
                for (const chunk of state.audioBuffer) {
                    sendAudioChunk(chunk);
                }
                state.audioBuffer = [];
                state.audioState = 'STREAMING';
                // When the tool eventually returns, handleToolCall() detects
                // audioState !== 'TOOL_PENDING' and sends with SILENT scheduling.
                // No timeout needed — see handleToolCall() logic below.
            }
            break;
    }
};
```

### Wire State Transitions to Existing Handlers

**1. turnComplete (line 403-407):** After existing logic, gate the mic:

```javascript
if (sc.turnComplete) {
    debugLog('done', 'TURN', 'Turn complete');
    state.currentAssistantMsg = null;
    state.audioState = 'IDLE'; // Start silent gating
}
```

**2. modelTurn (line 354-367):** When model starts speaking and we're in IDLE, enable barge-in:

```javascript
if (sc.modelTurn && sc.modelTurn.parts) {
    if (state.audioState === 'IDLE') {
        state.audioState = 'STREAMING'; // Allow barge-in during model speech
    }
    // ... existing audio playback code (unchanged)
}
```

**3. handleToolCall():** Modify the tool call handler:

- Only enter TOOL_PENDING for **Tier 1 tools** (not `mark_as_discussed`, `write_to_rules` — those are fire-and-forget with instant `{"status": "queued"}` responses)
- Set `state.audioState = 'TOOL_PENDING'` before sending request to backend
- When tool result returns from backend, there are exactly two cases:

```javascript
// Inside the async tool result handler
if (state.audioState === 'TOOL_PENDING') {
    // Normal path: user did not speak during the tool call
    sendToolResponseToGemini(response, 'WHEN_IDLE');
    state.audioState = 'STREAMING';
} else {
    // User spoke while tool was running (state is now STREAMING).
    // The tool may have taken 500ms or 5 seconds — doesn't matter.
    // Send immediately with SILENT: Gemini registers completion internally
    // without narrating or interrupting the active user speech.
    debugLog('info', 'TOOL', `Tool result arrived post-barge-in — sending with SILENT`);
    sendToolResponseToGemini(response, 'SILENT');
    // No state change — already STREAMING
}
```

This handles all timing combinations correctly:
- Tool fast (returns before user speaks): normal WHEN_IDLE path
- Tool fast (returns after user speaks): immediate SILENT send
- Tool slow (5s+, returns long after user spoke): immediate SILENT send — no timeout needed, no hanging session

**There is NO `state.pendingToolResponse` and NO timeout.** The pattern "send immediately with SILENT" replaces both. Remove `toolResponseTimeout` from the state object entirely.

**4. sendToolResponseToGemini() helper:** Extract the tool response sending into a helper that accepts an optional scheduling parameter:

```javascript
function sendToolResponseToGemini(responseData, scheduling = 'WHEN_IDLE') {
    const msg = {
        toolResponse: {
            functionResponses: [{
                id: responseData.id,
                name: responseData.name,
                response: responseData.response,
                scheduling: scheduling
            }]
        }
    };
    state.ws.send(JSON.stringify(msg));
}
```

**5. endSession():** Clear the audio buffer in endSession():

```javascript
state.audioBuffer = [];
state.audioState = 'IDLE';
```

---

## Phase 3: Fix generationComplete

**File:** `codiey/static/app.js`

**Line ~410** — add `'generationComplete'` to the `knownKeys` array:

```javascript
const knownKeys = ['interrupted', 'modelTurn', 'inputTranscription', 'outputTranscription', 'turnComplete', 'groundingMetadata', 'generationComplete'];
```

Add a handler before the unknown keys check:

```javascript
if (sc.generationComplete) {
    debugLog('done', 'GEN', 'Generation complete');
}
```

---

## Edge Cases & Notes

1. **First-syllable loss in IDLE→STREAMING:** The chunk that triggered energy detection IS sent (it's above threshold). Server-side VAD handles some leading silence naturally. Minimal impact.

2. **Buffer size during TOOL_PENDING:** At 2048 samples / 16kHz = 128ms per chunk. A 500ms tool call = ~4 chunks buffered. A 2s tool call = ~16 chunks. Flushing these in rapid succession is fine.

3. **Multiple tool calls in one batch:** If Gemini sends multiple function calls in one `toolCall` message, enter TOOL_PENDING once, execute all, send all responses together. The state machine handles the batch as a unit.

4. **Tier 2 tools never enter TOOL_PENDING:** `mark_as_discussed` and `write_to_rules` return `{"status": "queued"}` instantly and are sent with SILENT scheduling. No buffering needed.

5. **The 1008 crash is a separate server-side bug:** This plan improves UX around tool calls. For crash recovery, session resumption (Plan 2) is the fix. Consider testing with `gemini-2.5-flash-native-audio-preview-09-2025` for stability.

6. **ENERGY_THRESHOLD tuning:** 0.015 is a starting point. If false positives from background noise, increase to 0.02-0.03. If user speech is missed, decrease to 0.01. Can be made adaptive later.

7. **Tool timing edge case — why no timeout:** If the user speaks at second 1 and the tool returns at second 5, `audioState` is already `STREAMING` when the result arrives. The handler sends immediately with `SILENT` — Gemini registers the tool as complete without narrating. A timeout-based fallback creates a new race: the timeout fires at second 4 (before the tool returns), `pendingToolResponse` is null, it does nothing. Tool returns at second 5, stores in `pendingToolResponse`, which is never sent. By removing the timeout entirely and always sending immediately on arrival (with appropriate scheduling), all timing scenarios are handled identically with no race conditions.
