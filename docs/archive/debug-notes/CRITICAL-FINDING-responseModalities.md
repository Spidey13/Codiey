# CRITICAL FINDING: responseModalities Blocked Function Calling

## Discovery

After implementing the initial race condition fixes, testing revealed that the 1008 error was STILL occurring. Analysis of session log `2026-03-20T18-49-15-727Z.jsonl` revealed a completely different root cause.

## The Smoking Gun

**Session Log Evidence:**
```
Line 35: AI_SPEECH → "Sure, let me take a look at that."
Line 38: AI_SPEECH → "me take"
Line 49: AI_SPEECH → "read"
Line 51: AI_SPEECH → "through"
Line 53: AI_SPEECH → "`codiey/tools/handlers.py`"
Line 62: AI_SPEECH → "to summarize"
Line 65: AI_SPEECH → "the key"
Line 67: AI_SPEECH → "functions."
Line 68: WS_CLOSE → 1008 error
Line 68 extra: "audioState": "IDLE", "lastToolCall": null
```

**What This Tells Us:**
1. Model SAID it would read a file (line 53: "codiey/tools/handlers.py")
2. Model SAID it would "summarize the key functions" (lines 62-67)
3. **BUT NO TOOL_PRE LOG APPEARED** (would have been between lines 38-68)
4. **lastToolCall: null** - No tool was ever invoked
5. **audioState: "IDLE"** - State machine never entered TOOL_PENDING
6. Model crashed immediately after finishing its explanation

## The Bug

**Line 195 in `codiey/static/app.js`:**
```javascript
generationConfig: {
    responseModalities: ["AUDIO"],  // ← THIS WAS THE PROBLEM
    speechConfig: {
        voiceConfig: {
            prebuiltVoiceConfig: { voiceName: "Kore" }
        }
    }
},
```

## What `responseModalities: ["AUDIO"]` Does

According to Gemini Live API documentation:
- `responseModalities` specifies which output types the model can generate
- When set to `["AUDIO"]`, Gemini is **instructed to ONLY generate audio**
- This **completely blocks function call generation**
- The model's internal reasoning may decide to call a tool
- But the configuration prevents the function call from being emitted
- This creates an internal inconsistency in Gemini's state machine
- Result: WebSocket closes with error 1008

## Why This Caused the Crash

1. **User asks:** "Can you go through the code of handlers.py"
2. **Model's reasoning:** "I need to call read_file to access this file"
3. **Model's output generation:**
   - Speech: "Sure, let me take a look... I'll read through codiey/tools/handlers.py"
   - Function call: ❌ BLOCKED by responseModalities
4. **Gemini's internal state:** Inconsistent (wants to call tool, can't emit it)
5. **Result:** Internal error → 1008 WebSocket close

## The Fix

**Add TEXT modality to enable function calling:**
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

When responseModalities includes both AUDIO and TEXT, Gemini can generate:
- Audio responses (speech) ← AUDIO modality
- Function calls (tool invocations) ← TEXT modality
- Text responses (if needed) ← TEXT modality
- Any combination of the above

**Key insight:** Function calls are part of the TEXT modality, not AUDIO. You need both modalities enabled for voice + tools.

## Why This Wasn't Caught Earlier

The original session log (`2026-03-20T18-31-31-755Z.jsonl`) showed a different pattern:
- That crash happened at 11.3s after "help me understand the code of app"
- The model said "Let me read it to get a better understanding"
- **BUT it crashed before we could see if TOOL_PRE appeared**

We assumed it was the race condition bug (which WAS a real bug).
But the PRIMARY bug was that function calls were completely disabled.

## Verification

After removing `responseModalities: ["AUDIO"]`, the model should:
1. Generate audio speech as normal
2. **Also generate functionCall parts when needed**
3. Successfully invoke tools via the toolCall message
4. Log `TOOL_PRE` when functionCall stub appears
5. Complete tool execution without crashing

## Lessons Learned

1. **Configuration matters:** A single line can disable an entire feature
2. **Session logs are gold:** The absence of TOOL_PRE was the key clue
3. **Multiple root causes:** The race condition bug was real, but masked by this bigger bug
4. **Test incrementally:** After each fix, verify the specific behavior it's supposed to enable

## Related Documentation

- Gemini Live API: https://ai.google.dev/api/multimodal-live
- responseModalities: Controls output types (audio, text, function calls)
- When using tools, NEVER restrict responseModalities to ["AUDIO"]
