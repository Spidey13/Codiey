# Quick Fix Reference: 1008 Error Resolution (CORRECTED)

## The Complete Fix (2 Changes)

### 1. Remove Premature IDLE Reset (Line 573)
```javascript
if (sc.generationComplete) {
    debugLog('🏁', 'GEN', 'Generation complete');
    // Do NOT change audioState here — toolCall may be incoming
}
```

**Why:** `generationComplete` arrives BEFORE `toolCall`. Don't reset state too early or audio will leak through.

### 2. Strengthen Tool Detection (Line 521)
```javascript
if (part.functionCall) {
    state.audioState = 'TOOL_PENDING';
    debugLog('🔧', 'TOOL_PRE', `Detected functionCall stub, entering TOOL_PENDING`);
}
```

**Why:** Unconditional state change + debug logging ensures we always catch tool calls early.

### 3. responseModalities Setting
```javascript
responseModalities: ["AUDIO"], // ✅ CORRECT - Don't change this!
```

**IMPORTANT:** Do NOT use `["AUDIO", "TEXT"]` - it will cause **1007 "invalid argument"** error.

Function calling works perfectly with `["AUDIO"]` only. The API doesn't support multiple modalities simultaneously.

## What Was Wrong

The 1008 error was caused by **race condition bugs** in the audio state machine, NOT by the responseModalities configuration.

**Timeline of the bug:**
1. User asks to read a file
2. Gemini sends `generationComplete` message → code reset audioState to IDLE
3. Audio chunks start streaming again
4. Gemini sends `toolCall` message milliseconds later
5. Too late - audio already flowing during tool execution
6. Gemini rejects protocol violation → 1008 crash

## Testing

Start session and say: **"Can you go through the code of handlers.py"**

Expected behavior:
- ✅ Model says "Sure, let me read that"
- ✅ Console shows `🔧 TOOL_PRE: Detected functionCall stub, entering TOOL_PENDING`
- ✅ NO audio chunks between TOOL_PRE and TOOL_RESULT
- ✅ Tool executes successfully
- ✅ Model explains the file
- ✅ NO 1008 crash

## Error Messages Guide

| Error | Cause | Fix |
|-------|-------|-----|
| `1007 Cannot extract voices from non-audio request` | responseModalities doesn't include AUDIO | Use `["AUDIO"]` |
| `1007 Request contains invalid argument` | Multiple modalities like `["AUDIO", "TEXT"]` | Use only `["AUDIO"]` |
| `1008 Operation not implemented` | Race condition in state machine | Apply the 2 fixes above |

## The Correct Configuration

```javascript
generationConfig: {
    responseModalities: ["AUDIO"], // Only one modality allowed
    speechConfig: {
        voiceConfig: {
            prebuiltVoiceConfig: { voiceName: "Kore" }
        }
    }
}
```

- `AUDIO` → Enables voice responses AND function calls ✅
- Do NOT add `TEXT` → Causes 1007 error ❌
- Function calling works with AUDIO-only modality ✅
