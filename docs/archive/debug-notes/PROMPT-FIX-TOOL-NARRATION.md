# Prompt Fix: Model Talking About Tools But Not Calling Them

## Problem

Analysis of session log `2026-03-20T19-07-07-068Z.jsonl` revealed:
- User asked: "see a pattern or pass a dot py how does it work"
- Model responded: "Let me check those files for you. I'll start with `parser.py`."
- **NO tool was actually called** (no TOOL_PRE log)
- Session crashed with 1008 error
- `lastToolCall: null` confirmed no tool invocation

## Root Cause

The system prompt was too weak about tool usage:
```
"Never use uncertain language... If you are uncertain, say 'let me check' and use a tool."
```

This allowed the model to:
1. Say "let me check" (following the prompt)
2. But NOT actually call the tool
3. The prompt said "say AND use" but model only did the first part

## The Fix

**Strengthened the prompt with explicit rules and examples:**

### Before (Weak)
```javascript
Never use uncertain language — 'likely', 'probably', 'appears to be', 'suggests' — when a tool call can verify the answer. If you are uncertain, say "let me check" and use a tool. Never guess.

Tools: grep_search, file_search, list_directory, read_file, get_function_info, mark_as_discussed, write_to_rules
```

### After (Strong)
```javascript
CRITICAL TOOL USAGE RULES (READ CAREFULLY):
1. When user mentions a specific file name → IMMEDIATELY call read_file with that filename
2. When user asks about patterns, imports, or code structure → IMMEDIATELY call grep_search
3. When user asks "how does X work" and X is a file → IMMEDIATELY call read_file
4. NEVER say "let me check", "I'll look at", "I'll read", or "I'll search" WITHOUT calling the tool IN THE SAME RESPONSE
5. If you mention a file name in your response, you MUST call read_file for that file

BAD EXAMPLE (DON'T DO THIS):
User: "How does parser.py work?"
Bad Response: "Let me check that file for you." ❌ (talking about tool use, not using it)

GOOD EXAMPLE (DO THIS):
User: "How does parser.py work?"
Good Response: [Immediately calls read_file with file_path="parser.py"] ✅

Tools available: grep_search, file_search, list_directory, read_file, get_function_info, mark_as_discussed, write_to_rules
```

## Why This Works

1. **Numbered rules** - Concrete, actionable steps
2. **Explicit triggers** - "When user mentions X → do Y"
3. **Forbidden phrases** - Lists what NOT to say
4. **Bad/Good examples** - Shows contrast between narration vs action
5. **Visual indicators** - ❌ and ✅ reinforce the point
6. **Emphasis** - "CRITICAL", "IMMEDIATELY", "MUST" make it unmissable

## Expected Behavior Now

**User:** "How does parser.py work?"

**Model:** 
- [Calls read_file immediately]
- [Waits for tool result]
- "This file handles the AST parsing. It uses tree-sitter to..."

NO MORE:
- ❌ "Let me check that file"
- ❌ "I'll look at parser.py"
- ❌ "I'll read it for you"

## Testing

1. Reload page and start session
2. Say: **"How does handlers.py work?"**
3. Expected console output:
```
USER_SPEECH: "How does handlers.py work?"
🔧 TOOL_PRE: Detected functionCall stub, entering TOOL_PENDING
TOOL_CALL: 1 tool(s): read_file
TOOL_ARGS: read_file args=[file_path=handlers.py, reasoning=User asked how it works]
TOOL_RESULT: read_file → {...}
AI_SPEECH: "This file contains the tool execution handlers..."
```

4. NO 1008 crash!
5. Tool should be called BEFORE any narration

## Files Changed

- `codiey/static/app.js` lines 172-190: Strengthened system prompt

## Related Issues

This complements the 1008 race condition fixes. The 1008 error happens when:
1. Model narrates tool use without calling tool → no functionCall generated
2. Audio continues streaming while model is "thinking"
3. Gemini's state machine gets confused
4. WebSocket closes with 1008

By ensuring tools are ALWAYS called (not just narrated), we prevent this scenario entirely.

## Key Insight

**Models are literal.** If you say "say X and do Y", they might only do X. You must:
1. Make Y more important than X
2. Show concrete examples
3. Use strong imperative language
4. Provide visual/structural emphasis
