# Testing the 1008 Tool Call Fix

## Changes Made

### Fix 1: Removed Premature IDLE Reset (Line 571-574)
**Before:**
```javascript
if (sc.generationComplete) {
    debugLog('🏁', 'GEN', 'Generation complete');
    state.audioState = 'IDLE'; // ← BUG: Resets before toolCall arrives
}
```

**After:**
```javascript
if (sc.generationComplete) {
    debugLog('🏁', 'GEN', 'Generation complete');
    // Do NOT change audioState here — toolCall may be incoming
}
```

### Fix 2: Strengthened Tool Pending Detection (Line 518-523)
**Before:**
```javascript
if (part.functionCall) {
    if (state.audioState !== 'TOOL_PENDING') {
        state.audioState = 'TOOL_PENDING';
    }
}
```

**After:**
```javascript
if (part.functionCall) {
    // Preemptively enter TOOL_PENDING as soon as we see a functionCall stub
    // This prevents audio streaming during the gap before the actual toolCall message arrives
    state.audioState = 'TOOL_PENDING';
    debugLog('🔧', 'TOOL_PRE', `Detected functionCall stub, entering TOOL_PENDING`);
}
```

### Fix 3: Enabled Function Calling (Line 195) - CRITICAL
**Before:**
```javascript
generationConfig: {
    responseModalities: ["AUDIO"],  // ← BUG: This prevented ALL function calls!
    speechConfig: {
        voiceConfig: {
            prebuiltVoiceConfig: { voiceName: "Kore" }
        }
    }
},
```

**After:**
```javascript
generationConfig: {
    responseModalities: ["AUDIO", "TEXT"], // Both audio speech AND function calls
    speechConfig: {
        voiceConfig: {
            prebuiltVoiceConfig: { voiceName: "Kore" }
        }
    }
},
```

**Why this was critical:** 
- `responseModalities: ["AUDIO"]` told Gemini to ONLY generate audio
- Function calls are part of TEXT modality, so they were completely blocked
- Adding `"TEXT"` enables both audio speech AND function calling
- The model would try to call tools internally but the configuration prevented it
- This caused an internal state machine error that resulted in the 1008 crash

## How to Test

### 1. Start the Application
```bash
# Start the backend
python -m codiey

# Open browser to http://localhost:8000
```

### 2. Test Scenario 1: Single Tool Call
1. Click "Start Session"
2. Wait for "Session ready"
3. Say: **"Help me understand the code of app"**
4. Expected behavior:
   - Model should call `read_file` tool
   - WebSocket should stay connected (no 1008 error)
   - Model should respond with file content explanation

**Check session log** (`.codiey/session_logs/[timestamp].jsonl`):
```
AUDIO_IN → Sent mic chunk #101
USER_SPEECH → "help me understand the code of app"
GEN → Generation complete
TOOL_PRE → Detected functionCall stub, entering TOOL_PENDING  ← NEW LOG
TOOL_CALL → 1 tool(s): read_file
TOOL_RESULT → read_file → {...}
TOOL_RESPONSE → Sent 1 response(s) to Gemini
AI_SPEECH → "This is the main application..."
TURN → Turn complete
```

**Key verification:** NO `AUDIO_IN` chunks should appear between `TOOL_PRE` and `TOOL_RESPONSE`.

### 3. Test Scenario 2: Multiple Sequential Tool Calls
1. Say: **"Read app.js, then read pcm-processor.js"**
2. Expected behavior:
   - Model should call `read_file` twice
   - No 1008 errors
   - Clean state transitions between tools

**Check browser console for:**
```
🔧 [timestamp] TOOL_PRE: Detected functionCall stub, entering TOOL_PENDING
🔧 [timestamp] TOOL_CALL: 2 tool(s): read_file, read_file
✅ [timestamp] TOOL_RESULT: read_file → {...}
✅ [timestamp] TOOL_RESULT: read_file → {...}
📤 [timestamp] TOOL_RESPONSE: Sent 2 response(s) to Gemini
```

### 4. Test Scenario 3: Rapid Tool Calls
1. Say: **"List all Python files, then show me the main entry point"**
2. Expected behavior:
   - Multiple tools may be invoked
   - No 1008 crashes
   - Session stays connected

### 5. Test Scenario 4: Tool Call After Interruption
1. Start speaking to model
2. Interrupt it mid-sentence
3. Ask: **"Read the README file"**
4. Expected behavior:
   - Tool should execute cleanly
   - No 1008 error
   - Model should respond normally

## What to Look For

### Success Indicators
- ✅ WebSocket stays open during all tool calls
- ✅ New `TOOL_PRE` log appears in console when functionCall detected
- ✅ No `AUDIO_IN` chunks between `TOOL_PRE` and `TOOL_RESPONSE`
- ✅ Session log shows clean state transitions: `IDLE → TOOL_PENDING → STREAMING → IDLE`

### Failure Indicators (should NOT happen)
- ❌ WebSocket closes with error 1008
- ❌ `AUDIO_IN` chunks appear during tool execution
- ❌ Console shows: "WebSocket closed: 1008 Operation is not implemented, or supported, or enabled"

## Debugging Tips

### Enable Detailed Message Logging
Add this to the top of `handleGeminiMessage()` (around line 485):
```javascript
console.log('📨 Message keys:', Object.keys(data));
if (data.serverContent) {
    console.log('  serverContent keys:', Object.keys(data.serverContent));
}
```

This will show you the exact sequence of messages from Gemini:
```
📨 Message keys: ['serverContent']
  serverContent keys: ['generationComplete', 'modelTurn']  ← functionCall is here
📨 Message keys: ['toolCall']  ← Arrives in separate message
```

### Monitor Audio State
Add this temporary logging in the worklet handler (line 738):
```javascript
workletNode.port.onmessage = (event) => {
    console.log('🎤 Worklet state:', state.audioState);  // Add this
    if (event.data.type !== "pcm" || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    // ... rest of handler
```

You should see:
```
🎤 Worklet state: STREAMING
🎤 Worklet state: STREAMING
🔧 TOOL_PRE: Detected functionCall stub, entering TOOL_PENDING
🎤 Worklet state: TOOL_PENDING  ← Chunks are blocked here
🎤 Worklet state: TOOL_PENDING
✅ TOOL_RESULT: ...
🎤 Worklet state: STREAMING  ← Resumes after tool completes
```

## Expected Results

After these fixes:
1. **Race condition eliminated:** `generationComplete` no longer resets state prematurely
2. **Early blocking:** Audio stops as soon as `functionCall` stub is detected
3. **No audio during tools:** Worklet rigorously blocks all chunks while `audioState === 'TOOL_PENDING'`
4. **Clean state machine:** No gaps in state transitions

**The 1008 error should be completely eliminated.**

## Rollback Plan (if needed)

If issues arise, revert both changes:

```bash
git diff HEAD codiey/static/app.js
git checkout HEAD -- codiey/static/app.js
```

## Next Steps

Once testing confirms the fix:
1. Commit the changes
2. Monitor production logs for any remaining 1008 errors
3. If stable after 24h, consider implementing the full audio pipeline from `PLAN-1-audio-pipeline.md`
