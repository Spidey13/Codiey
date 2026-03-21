# Plan 2: Context Management & Session Resilience

> Priority: HIGH — enables unlimited session length and crash recovery
> Files: `codiey/static/app.js`

---

## Current State

- Setup message has NO `contextWindowCompression` — sessions limited to ~15 minutes for audio, often crash earlier
- Setup message has NO `sessionResumption` — every connection drop kills the session permanently
- `sessionResumptionUpdate` tokens are stored in localStorage but never used on reconnect
- `goAway` is in `knownTopKeys` but has zero handling logic — server warns before closing and we ignore it
- `ws.onclose` always calls `endSession()` — no reconnection attempt
- Rules file content lives in `systemInstruction` via `summary_builder.py` — this is correct and survives sliding window compression

---

## Phase 1: Enable Context Window Compression

**File:** `codiey/static/app.js` — `sendSetupMessage()` function (line 150)

Add `contextWindowCompression` to the setup message object. This tells Gemini to automatically prune oldest turns when context grows large, instead of crashing with 1008.

**Add to the `setupMessage.setup` object** (after `outputAudioTranscription: {}`):

```javascript
contextWindowCompression: {
    slidingWindow: {}
},
```

The `slidingWindow: {}` uses default trigger. System instruction (which contains rules.md content) survives compression forever. Tool results and conversation history get pruned oldest-first automatically. No manual token management needed.

---

## Phase 2: Enable Session Resumption in Setup

**File:** `codiey/static/app.js` — `sendSetupMessage()` function

The server already sends `sessionResumptionUpdate` tokens and we already store them in localStorage (line 438-444). But we never send the handle back when connecting. Fix this.

**Add to the `setupMessage.setup` object:**

```javascript
sessionResumption: resumptionConfig,
```

**Before building the setup message**, compute the config:

```javascript
const storedHandle = localStorage.getItem('codiey_resumption_token');
const resumptionConfig = storedHandle ? { handle: storedHandle } : {};
```

**Important:** Clear stale handles on successful new session start. In `onSessionReady()` (line 447), after a fresh connection (not a resumption), consider clearing the old handle:

```javascript
// If this was a fresh session (not resumption), the old handle is stale
if (!storedHandle) {
    localStorage.removeItem('codiey_resumption_token');
}
```

And in `startSession()`, before opening the WebSocket, add a `state.isResuming` flag based on whether we have a stored handle, so `onSessionReady()` can distinguish fresh vs resumed sessions.

---

## Phase 3: Handle GoAway Messages

**File:** `codiey/static/app.js` — `handleGeminiMessage()` function

`goAway` is already in `knownTopKeys` (line 428) but has no logic. The server sends this ~60 seconds before forcibly closing. Use it to reconnect gracefully.

**Add after the `sessionResumptionUpdate` handler (after line 444):**

```javascript
if (data.goAway) {
    const timeLeft = data.goAway.timeLeft || 'unknown';
    debugLog('warn', 'GOAWAY', `Server will close connection in ${timeLeft}`);

    if (state.sessionLogger) {
        state.sessionLogger.log('GOAWAY', `Server will close in ${timeLeft}`, { timeLeft });
    }

    // Proactively reconnect before forced close
    addSystemMessage('Reconnecting seamlessly...');
    attemptGracefulReconnect();
}
```

---

## Phase 4: Graceful Reconnection Logic

**File:** `codiey/static/app.js` — new functions

Add a `state.reconnecting` flag (default: `false`) to the state object.

### attemptGracefulReconnect()

```javascript
async function attemptGracefulReconnect() {
    const handle = localStorage.getItem('codiey_resumption_token');
    if (!handle) {
        debugLog('warn', 'RECONNECT', 'No resumption handle — cannot reconnect');
        return;
    }

    state.reconnecting = true;

    // Close current WebSocket cleanly (don't trigger endSession)
    if (state.ws) {
        state.ws.onclose = null; // Detach handler to prevent endSession
        state.ws.close(1000, 'Graceful reconnect');
        state.ws = null;
    }

    try {
        const keyRes = await fetch('/api/key');
        if (!keyRes.ok) throw new Error('Failed to fetch API key');
        const { key } = await keyRes.json();

        const wsUrl = `${GEMINI_WS_BASE_URL}?key=${key}`;
        state.ws = new WebSocket(wsUrl);

        state.ws.onopen = () => {
            debugLog('ok', 'RECONNECT', 'WebSocket reconnected — sending setup with resumption handle');
            sendSetupMessage(); // Will include the handle from localStorage
        };

        state.ws.onmessage = async (event) => {
            let data;
            if (event.data instanceof Blob) {
                data = JSON.parse(await event.data.text());
            } else {
                data = JSON.parse(event.data);
            }
            handleGeminiMessage(data);
        };

        state.ws.onerror = (err) => {
            console.error('Reconnection WebSocket error:', err);
            state.reconnecting = false;
        };

        state.ws.onclose = (event) => {
            handleWebSocketClose(event);
        };

        state.reconnecting = false;

    } catch (err) {
        console.error('Reconnection failed:', err);
        state.reconnecting = false;
        addSystemMessage('Reconnection failed — session ended');
        endSession();
    }
}
```

### Refactor ws.onclose Into a Named Function

Extract the current inline `ws.onclose` handler (lines 127-142) into a named function so both the initial connection and reconnections use the same logic:

```javascript
function handleWebSocketClose(event) {
    // Don't trigger during intentional reconnect
    if (state.reconnecting) return;

    console.log("WebSocket closed:", event.code, event.reason);

    if (state.sessionLogger) {
        state.sessionLogger.log('WS_CLOSE', `WebSocket closed: ${event.code} ${event.reason}`, {
            code: event.code,
            reason: event.reason,
            wasClean: event.wasClean,
        });
        state.sessionLogger.flush();
    }

    // If session was active and we have a resumption handle, try auto-reconnect
    // Don't reconnect on clean close (1000) — that's intentional
    const handle = localStorage.getItem('codiey_resumption_token');
    if (state.sessionActive && handle && event.code !== 1000) {
        debugLog('warn', 'RECONNECT', `Unexpected close (${event.code}), attempting reconnect...`);
        addSystemMessage('Connection lost — reconnecting...');
        attemptGracefulReconnect();
        return;
    }

    if (state.sessionActive) {
        endSession();
        addSystemMessage("Connection lost");
    }
}
```

Wire this in the initial `startSession()` connection:

```javascript
state.ws.onclose = (event) => {
    handleWebSocketClose(event);
};
```

---

## Phase 5: Handle Stale Resumption Handles

Resumption handles expire after 2 hours. If we try to reconnect with a stale handle, the server rejects with 1008 "not found". Handle this:

In `handleWebSocketClose()`, if the reconnection itself fails with 1008, clear the stale handle and start a fresh session:

```javascript
// Inside handleWebSocketClose, if auto-reconnect was attempted but failed with 1008:
if (event.code === 1008 && state.reconnecting) {
    debugLog('warn', 'RECONNECT', 'Resumption handle rejected (expired?) — starting fresh session');
    localStorage.removeItem('codiey_resumption_token');
    state.reconnecting = false;
    // Could auto-start a fresh session here, or just end and let user restart
    endSession();
    addSystemMessage('Session expired — please start a new session');
    return;
}
```

---

## Phase 6: Clean Up endSession for Reconnection Awareness

**File:** `codiey/static/app.js` — `endSession()` function

The current `endSession()` (line 208) tears down audio capture, WebSocket, and logger. When reconnecting, we do NOT want to tear down audio capture (keep the mic alive across reconnections as per best practices).

Add a parameter or check:

```javascript
async function endSession(keepAudio = false) {
    // Only tear down audio if this is a real session end, not a reconnect
    if (!keepAudio) {
        if (state.audioCapture) {
            state.audioCapture.stop();
            state.audioCapture = null;
        }
        if (state.audioPlayer) {
            state.audioPlayer.flush();
            state.audioPlayer.audioContext.close();
            state.audioPlayer = null;
        }
    }

    // ... rest of existing cleanup (logger, backend drain, etc.)
}
```

In `attemptGracefulReconnect()`, if you need to clean up before reconnecting, call `endSession(true)` to keep audio alive.

---

## What Survives Sliding Window Compression

| Content | Location | Survives? |
|---------|----------|-----------|
| Rules file (.codiey/rules) | systemInstruction | Yes, always |
| Fixed instructions (persona, behavior) | systemInstruction | Yes, always |
| Directory tree | systemInstruction | Yes, always |
| Tool results | Turn history | Pruned oldest-first |
| User/AI speech | Turn history | Pruned oldest-first |

---

## Future: rules.md Compaction (v2)

The rules file grows as `write_to_rules` appends insights. Eventually it bloats the system instruction. A future compaction pass should:
- Merge duplicate insights
- Remove superseded facts
- Keep the file under a token budget (e.g., 2000 tokens)
- Run periodically (on session start, or when file exceeds threshold)

Not needed for v1 but should be on the roadmap.

---

## Summary of Changes

1. Add `contextWindowCompression: { slidingWindow: {} }` to setup message
2. Add `sessionResumption: {}` (or `{ handle: storedHandle }`) to setup message
3. Handle `goAway` messages — trigger graceful reconnect
4. Add `attemptGracefulReconnect()` function with full WebSocket re-establishment
5. Refactor `ws.onclose` into `handleWebSocketClose()` with auto-reconnect on unexpected close
6. Handle stale resumption handles (clear and start fresh)
7. Keep audio capture alive across reconnections
