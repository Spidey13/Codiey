# Additional Fix: VAD Settings Too Aggressive

## Problem

After fixing the 1008 errors, audio input wasn't reaching Gemini. Console showed:
- VAD detecting speech start/end repeatedly
- NO "Sent mic chunk" messages
- Speech segments ending too quickly

## Root Cause

The VAD (Voice Activity Detection) settings were too strict:
- `redemptionFrames: 12` - Too short grace period
- `negativeSpeechThreshold: 0.60` - Too high (ends speech too easily)
- `positiveSpeechThreshold: 0.85` - Too strict to start

Result: Speech segments ended before enough audio accumulated to send to Gemini.

## The Fix

### Changed VAD Settings (Line 711-715)

**Before:**
```javascript
positiveSpeechThreshold: 0.85,  // Very strict
negativeSpeechThreshold: 0.60,  // Ends too easily
minSpeechFrames: 4,
preSpeechPadFrames: 5,
redemptionFrames: 12,           // Too short
```

**After:**
```javascript
positiveSpeechThreshold: 0.80,  // Slightly easier to trigger
negativeSpeechThreshold: 0.40,  // Keeps speech active longer
minSpeechFrames: 3,             // Starts faster
preSpeechPadFrames: 10,         // More context
redemptionFrames: 25,           // Much longer grace period
```

### Added Debug Logging (Line 748-750)

```javascript
// Debug: Log the state to understand why audio isn't flowing
if (state.audioChunksSent === 0 || state.audioChunksSent % 100 === 1) {
    console.log(`🎤 Worklet state: isUserSpeaking=${state.isUserSpeaking}, audioState=${state.audioState}`);
}
```

## Why These Settings Work

1. **redemptionFrames: 25** - Allows ~400ms of silence before ending speech (was ~200ms)
2. **negativeSpeechThreshold: 0.40** - Requires lower confidence to end, so pauses don't end speech
3. **positiveSpeechThreshold: 0.80** - Easier to start speaking (was missing initial syllables)
4. **preSpeechPadFrames: 10** - Captures more context before detected speech

## Expected Behavior Now

When you speak:
1. Console shows: `🗣️ VAD: Speech STARTED`
2. Console shows: `🎤 Worklet state: isUserSpeaking=true, audioState=STREAMING`
3. Console shows: `📤 AUDIO_IN: Sent mic chunk #1`
4. Audio flows continuously while speaking
5. After you finish: `⏹️ VAD: Speech ENDED` (after longer pause)

## Testing

1. Reload the page
2. Start session
3. Say: **"Hello, can you hear me?"**
4. You should see audio chunks being sent
5. Model should respond

## Files Changed

- `codiey/static/app.js`:
  - Lines 711-715: VAD settings adjusted
  - Lines 748-750: Debug logging added

## Tuning Guide

If you still have issues:

**Too many false starts:** Increase `positiveSpeechThreshold` (0.80 → 0.85)
**Speech cuts off too early:** Increase `redemptionFrames` (25 → 30 or 35)
**Background noise triggers:** Increase `positiveSpeechThreshold`, decrease `preSpeechPadFrames`
**First syllable missed:** Increase `preSpeechPadFrames` (10 → 15)
