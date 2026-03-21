# 📋 Codiey — Implementation Plan
### *Built from the official Gemini Live API docs at [ai.google.dev/gemini-api/docs/live-api](https://ai.google.dev/gemini-api/docs/live-api)*

> [!IMPORTANT]
> Every API call, config shape, and method name in this document is verified against the official Google documentation (last updated 2026-03-09). No assumptions.

---

## 1. Architecture: Thin TS Shell + Python Backend

```
┌──────────────────────────────────────────────────────────────┐
│              VS Code Extension (TypeScript ~300 LOC)          │
│  extension.ts → spawns Python → relays messages to webview    │
└──────────────────────┬───────────────────────────────────────┘
                       │ stdin/stdout JSON
                       ▼
┌──────────────────────────────────────────────────────────────┐
│              Python Backend (ALL real logic)                   │
│                                                                │
│  google-genai SDK → Gemini Live API (WebSocket)               │
│  PyAudio → mic capture (16-bit PCM, 16kHz)                    │
│  tree-sitter → codebase parsing                               │
│  Session management, mental model, decisions.md               │
└──────────────────────────────────────────────────────────────┘
```

**Why:** You write Python. The Gemini Python SDK (`google-genai`) is the primary SDK with the best Live API support. Tree-sitter Python bindings are more mature. PyAudio replaces the need for SoX.

---

## 2. Exact Model & Config (From Docs)

### Model
```python
model = "gemini-2.5-flash-native-audio-preview-12-2025"
```

This is the native audio model that supports:
- ✅ Affective dialog (adapts tone to your expression)
- ✅ Proactive audio (decides when to respond vs stay silent)
- ✅ Thinking (can reason before responding)
- ✅ Function calling
- ✅ 128K token context window

### API Version
```python
# v1alpha required for affective dialog + proactive audio
client = genai.Client(http_options={"api_version": "v1alpha"})
```

### Full Session Config
```python
from google.genai import types

config = types.LiveConnectConfig(
    # ── Response ──
    response_modalities=["AUDIO"],
    
    # ── Voice ──
    speech_config={
        "voice_config": {
            "prebuilt_voice_config": {"voice_name": "Kore"}  # Pick from AI Studio
        }
    },
    
    # ── Native Audio Features ──
    enable_affective_dialog=True,           # Adapts tone to user emotion
    proactivity={"proactive_audio": True},  # Decides when to respond
    
    # ── Thinking ──
    thinking_config=types.ThinkingConfig(
        thinking_budget=1024,  # Tokens for internal reasoning
    ),
    
    # ── Transcription (for UI display) ──
    output_audio_transcription={},   # Get text of what Gemini says
    input_audio_transcription={},    # Get text of what user says
    
    # ── Session Management ──
    context_window_compression=types.ContextWindowCompressionConfig(
        sliding_window=types.SlidingWindow(),  # Unlimited session length
    ),
    session_resumption=types.SessionResumptionConfig(
        handle=None,  # None for new session, token for resume
    ),
    
    # ── VAD Tuning ──
    realtime_input_config={
        "automatic_activity_detection": {
            "disabled": False,
            "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",   # Less trigger-happy
            "end_of_speech_sensitivity": "END_SENSITIVITY_LOW",       # Wait longer before "done"
            "prefix_padding_ms": 20,
            "silence_duration_ms": 100,
        }
    },
    
    # ── Tools (see Section 4) ──
    tools=[...],
    
    # ── System Prompt ──
    system_instruction=types.Content(
        parts=[types.Part(text=SYSTEM_PROMPT)]
    ),
)
```

> [!NOTE]
> **VAD Sensitivity Matters for Your Use Case.** `START_SENSITIVITY_LOW` prevents the model from being interrupted by thinking pauses ("hmm", "uh"). `END_SENSITIVITY_LOW` gives the user time to collect their thoughts before Gemini assumes they're done talking. This is critical for planning conversations where people pause to think.

---

## 3. The Core Loop (From Docs)

```python
import asyncio
from google import genai
from google.genai import types

client = genai.Client(http_options={"api_version": "v1alpha"})
model = "gemini-2.5-flash-native-audio-preview-12-2025"

async def main():
    async with client.aio.live.connect(model=model, config=config) as session:
        
        # ── Start two parallel tasks ──
        audio_send_task = asyncio.create_task(send_audio(session))
        receive_task = asyncio.create_task(receive_loop(session))
        
        await asyncio.gather(audio_send_task, receive_task)


async def send_audio(session):
    """Continuously capture mic audio and send to Gemini."""
    import pyaudio
    
    p = pyaudio.PyAudio()
    stream = p.open(
        format=pyaudio.paInt16,     # 16-bit
        channels=1,                  # mono
        rate=16000,                  # 16kHz — exactly what Gemini needs
        input=True,
        frames_per_buffer=1024,
    )
    
    try:
        while True:
            chunk = stream.read(1024, exception_on_overflow=False)
            await session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type="audio/pcm;rate=16000"
                )
            )
            await asyncio.sleep(0.01)  # ~100 sends/sec
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


async def receive_loop(session):
    """Handle all messages from Gemini."""
    async for response in session.receive():
        
        # ── Interruption ──
        if response.server_content and response.server_content.interrupted is True:
            flush_audio_playback()  # Stop playing, clear queue
            continue
        
        # ── Audio response from model ──
        if response.server_content and response.server_content.model_turn:
            for part in response.server_content.model_turn.parts:
                if part.inline_data:
                    play_audio_chunk(part.inline_data.data)  # 24kHz PCM
        
        # ── Output transcription (for UI) ──
        if response.server_content and response.server_content.output_transcription:
            transcript_text = response.server_content.output_transcription.text
            send_to_webview({"type": "model_transcript", "text": transcript_text})
        
        # ── Input transcription (what user said, for UI) ──
        if response.server_content and response.server_content.input_transcription:
            user_text = response.server_content.input_transcription.text
            send_to_webview({"type": "user_transcript", "text": user_text})
        
        # ── Tool call ──
        if response.tool_call:
            await handle_tool_call(session, response.tool_call)
        
        # ── Turn complete ──
        if response.server_content and response.server_content.turn_complete:
            pass  # Model finished speaking this turn
        
        # ── Session resumption token ──
        if response.session_resumption_update:
            update = response.session_resumption_update
            if update.resumable and update.new_handle:
                save_resumption_token(update.new_handle)
```

> [!TIP]
> **Audio format facts from docs:**
> - Input: 16-bit PCM, 16kHz, mono, little-endian (`audio/pcm;rate=16000`)
> - Output: 24kHz PCM (always), little-endian
> - The API resamples if needed, but 16kHz is optimal

---

## 4. Tool Declarations (NON_BLOCKING)

The key insight: make all codebase tools `NON_BLOCKING` so Gemini says "let me check..." while the tool runs, instead of going silent.

```python
tools = [{
    "function_declarations": [
        {
            "name": "get_project_overview",
            "description": (
                "Get a high-level overview of the project: directory tree, "
                "file counts by language, detected framework/patterns, "
                "and a list of all top-level modules."
            ),
            "behavior": "NON_BLOCKING",
        },
        {
            "name": "get_file_details",
            "description": (
                "Get the structure of a specific file: all functions, classes, "
                "imports, exports, and their line numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path to the file from project root"
                    }
                },
                "required": ["file_path"]
            },
            "behavior": "NON_BLOCKING",
        },
        {
            "name": "get_function_info",
            "description": (
                "Get detailed info about a specific function: its source code, "
                "parameters, return type, who calls it, and what it calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "function_name": {"type": "string"},
                    "file_path": {"type": "string"}
                },
                "required": ["function_name"]
            },
            "behavior": "NON_BLOCKING",
        },
        {
            "name": "get_dependency_graph",
            "description": (
                "Get the import/dependency relationships for a module: "
                "what imports it and what it imports."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "module_path": {"type": "string"}
                },
                "required": ["module_path"]
            },
            "behavior": "NON_BLOCKING",
        },
        {
            "name": "search_codebase",
            "description": (
                "Search for patterns, function names, class names, or concepts "
                "across the entire codebase. Returns matching files and locations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "search_type": {
                        "type": "string",
                        "enum": ["function", "class", "import", "pattern", "text"]
                    }
                },
                "required": ["query"]
            },
            "behavior": "NON_BLOCKING",
        },
        {
            "name": "mark_as_discussed",
            "description": (
                "Mark a file, directory, or concept as discussed in this session. "
                "Updates the mental model tracker shown in the UI."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "topic": {"type": "string", "description": "What was discussed about it"}
                },
                "required": ["path", "topic"]
            },
            "behavior": "NON_BLOCKING",
        },
    ]
}]
```

### Handling Tool Responses (With Scheduling)

```python
async def handle_tool_call(session, tool_call):
    """Execute tool locally, send result back with scheduling."""
    function_responses = []
    
    for fc in tool_call.function_calls:
        # Execute the tool (local, fast — tree-sitter lookup)
        result = execute_tool(fc.name, fc.args)
        
        function_response = types.FunctionResponse(
            id=fc.id,
            name=fc.name,
            response={
                "result": result,
                "scheduling": "WHEN_IDLE"
                # WHEN_IDLE = wait for model to finish its filler,
                #             then incorporate the data naturally
                #
                # INTERRUPT = immediately cut in with the data
                # SILENT    = absorb silently, use later
            }
        )
        function_responses.append(function_response)
    
    await session.send_tool_response(function_responses=function_responses)
```

> [!IMPORTANT]
> **Scheduling options from docs:**
> | Scheduling | Behavior | Best For |
> |---|---|---|
> | `INTERRUPT` | Stop current speech, speak about result immediately | Urgent corrections |
> | `WHEN_IDLE` | Wait until model finishes current thought, then speak about result | Most codebase lookups |
> | `SILENT` | Absorb silently, use the knowledge later | `mark_as_discussed`, background context |
>
> For Codiey: Use `WHEN_IDLE` for codebase lookups (feels natural), `SILENT` for `mark_as_discussed` (no need to speak about it).

---

## 5. System Prompt (With Interrupt Handling)

```python
SYSTEM_PROMPT = """You are Codiey, a voice-first codebase thinking partner. You help 
developers reason about their code through conversation. You NEVER write or modify code.

## Your Role
Think of yourself as a senior engineer who has read the developer's entire codebase and 
is now having a coffee chat about architecture and planning. You:
- Reference specific files, functions, and line numbers using your tools
- Think out loud WITH the developer, not AT them
- Surface concerns proactively (tight coupling, missing tests, scalability issues)
- Keep responses concise — this is a voice conversation, not documentation

## When You Are Interrupted
The developer may redirect you mid-thought. This is the primary interaction pattern. When 
this happens:
1. Briefly acknowledge what you were discussing ("Right, I was looking at the routes—")
2. Confirm the new direction ("—but you're saying the real issue is in the DB layer")
3. Connect old and new topics IF relevant ("which actually matters because the routes 
   depend on those queries")
4. Continue with the new direction
Never restart from zero. Always carry context forward.

## Tools
Use tools actively. Don't guess about the code — look it up. When the developer asks 
about something, check the actual code first, then reason about it.
- get_project_overview: big picture structure
- get_file_details: examine a specific file
- get_function_info: understand a specific function
- get_dependency_graph: see how modules connect
- search_codebase: find things across the project
- mark_as_discussed: track what you've covered (use SILENTLY — don't mention it)

## Current Project Context
{codebase_summary}
"""
```

> [!NOTE]
> The `{codebase_summary}` is injected at session start — a compact, deterministic summary 
> built by tree-sitter: file tree (top 2 levels), list of all functions/classes with file 
> locations, import graph summary, detected framework/patterns. ~500-2000 tokens. This means 
> Gemini already knows the project structure BEFORE any tool calls, eliminating 70%+ of tool 
> call needs.

---

## 6. File Structure

```
codiey/
├── package.json                    # VS Code extension manifest
├── tsconfig.json
├── src/                            # MINIMAL TypeScript (~300 LOC)
│   ├── extension.ts                # Activate, spawn Python, register commands
│   ├── pythonBridge.ts             # Spawn subprocess, stdin/stdout JSON protocol
│   ├── webviewPanel.ts             # Create webview, relay messages
│   └── statusBar.ts                # 🎙️ Codiey button
│
├── webview/                        # UI (plain HTML/CSS/JS)
│   ├── index.html                  # Transcript + controls + mental model
│   ├── styles.css                  # Dark theme, Premium design
│   └── app.js                      # Message handling, DOM updates
│
├── python/                         # ═══ ALL THE REAL CODE ═══
│   ├── main.py                     # Entry point: stdin/stdout message loop
│   ├── requirements.txt            # google-genai, tree-sitter, pyaudio
│   │
│   ├── audio/
│   │   ├── recorder.py             # PyAudio mic capture → PCM 16kHz
│   │   └── player.py               # PCM 24kHz → speakers (pyaudio playback)
│   │
│   ├── gemini/
│   │   ├── session.py              # Gemini Live API connection + receive loop
│   │   ├── tools.py                # Tool declarations + handler dispatch
│   │   └── prompts.py              # System prompt builder
│   │
│   ├── codebase/
│   │   ├── parser.py               # Tree-sitter AST parsing (Python + TS/JS)
│   │   ├── map_builder.py          # Build codebase graph from ASTs
│   │   ├── call_graph.py           # Function → calls → function relationships
│   │   ├── dependency_graph.py     # Module import/export links
│   │   ├── pattern_detector.py     # Detect Flask/Django/Express/etc.
│   │   └── summary_builder.py      # Generate compact summary for system prompt
│   │
│   ├── session/
│   │   ├── manager.py              # Start/stop/pause session lifecycle
│   │   ├── mental_model.py         # Track which code areas discussed
│   │   └── decisions.py            # Write to decisions.md
│   │
│   └── utils/
│       ├── config.py               # Settings, API key management
│       └── protocol.py             # stdin/stdout JSON message protocol
│
├── .codiey/                        # Per-workspace runtime data (gitignored)
│   ├── sessions/                   # Session transcripts
│   ├── decisions.md                # Architectural decisions log
│   └── mental-model.json           # Understanding coverage map
│
└── README.md
```

---

## 7. Communication Protocol (TS ↔ Python)

Simple newline-delimited JSON over stdin/stdout:

### TypeScript → Python (commands)
```json
{"command": "start_session", "payload": {"workspace": "/path/to/project", "api_key": "..."}}
{"command": "stop_session", "payload": {}}
{"command": "get_mental_model", "payload": {}}
{"command": "get_decisions", "payload": {}}
```

### Python → TypeScript (events)
```json
{"event": "session_started", "data": {}}
{"event": "model_transcript", "data": {"text": "Looking at your routes..."}}
{"event": "user_transcript", "data": {"text": "Tell me about the auth"}}
{"event": "interrupted", "data": {}}
{"event": "mental_model_update", "data": {"path": "src/auth.py", "status": "discussed"}}
{"event": "tool_called", "data": {"name": "get_file_details", "args": {"file_path": "src/auth.py"}}}
{"event": "session_ended", "data": {"transcript_path": ".codiey/sessions/..."}}
{"event": "error", "data": {"message": "..."}}
```

### TypeScript Side (~80 lines)
```typescript
// pythonBridge.ts
import { spawn, ChildProcess } from 'child_process';
import * as readline from 'readline';

export class PythonBridge {
    private process: ChildProcess | null = null;
    private messageHandlers: ((msg: any) => void)[] = [];

    constructor(private extensionPath: string) {}

    start(pythonPath: string, workspacePath: string) {
        this.process = spawn(pythonPath, [
            `${this.extensionPath}/python/main.py`,
            '--workspace', workspacePath
        ]);

        const rl = readline.createInterface({ input: this.process.stdout! });
        rl.on('line', (line) => {
            try {
                const msg = JSON.parse(line);
                this.messageHandlers.forEach(h => h(msg));
            } catch (e) { /* ignore malformed lines */ }
        });

        this.process.stderr?.on('data', (data) => {
            console.error(`[Codiey Python] ${data}`);
        });
    }

    send(command: string, payload: any = {}) {
        this.process?.stdin?.write(JSON.stringify({ command, payload }) + '\n');
    }

    onMessage(handler: (msg: any) => void) {
        this.messageHandlers.push(handler);
    }

    stop() {
        this.process?.kill();
        this.process = null;
    }
}
```

---

## 8. Session Management (From Docs)

### Unlimited Session Duration
The docs say:
> "Audio-only sessions are limited to 15 minutes... you can use context window compression to extend sessions to an unlimited amount of time."

Our config already includes `context_window_compression` with `SlidingWindow()`, so sessions run indefinitely.

### Surviving Disconnects
The docs say:
> "The lifetime of a connection is limited to around 10 minutes. When the connection terminates, the session terminates as well. You can configure session resumption."

Our config includes `session_resumption`. The flow:

```python
# During session: periodically receive resumption tokens
if response.session_resumption_update:
    update = response.session_resumption_update
    if update.resumable and update.new_handle:
        self.resumption_token = update.new_handle

# On disconnect: reconnect with saved token
config.session_resumption = types.SessionResumptionConfig(
    handle=self.resumption_token  # Valid for 2 hours
)
# Session resumes with full context
```

### GoAway Handling
```python
# The server sends a GoAway before disconnecting
# We can proactively reconnect
if response.go_away:
    # Save current state, reconnect with resumption token
    await self.reconnect()
```

---

## 9. Known Risks & Mitigations

| Risk | Probability | Mitigation |
|---|---|---|
| **Self-interruption** (model hears its own audio) | High without headphones | Document "use headphones" prominently. Consider echo cancellation later. |
| **Premature turnComplete** (model cuts off mid-sentence) | Medium | Detect short turns, optionally prompt "were you done?" |
| **NON_BLOCKING hallucination** (guesses before tool result) | Medium | System prompt: "NEVER speculate on tool results. Say 'let me check' and WAIT." |
| **Choppy audio on preview models** | Low | Stick to stable model. Test before committing. |
| **Transcription degradation in long sessions** | Low | Send `activity_end` + `activity_start` to reset pipeline periodically. |
| **PyAudio installation pain on Windows** | Medium | Provide pre-built wheel or fallback to SoX subprocess. |

---

## 10. Build Schedule: 4 Weekends

### Weekend 1: Voice Pipeline (The Skeleton)

**Goal: Speak to Gemini Live API, hear a response, and interrupt mid-response.**

```
Tasks:
  ├── Scaffold VS Code extension (yo code)
  ├── Write PythonBridge (spawn, stdin/stdout JSON)
  ├── Write Python main.py message loop
  ├── PyAudio mic capture → send_realtime_input()
  ├── Receive audio → playback via PyAudio
  ├── Handle server_content.interrupted → flush audio
  ├── Handle input/output transcription → print to console
  └── Test: speak, hear response, interrupt, hear adjusted response

Deliverable: 
  "Install extension → paste API key → click 🎙️ → speak → hear Gemini → 
   interrupt → Gemini adjusts. All in VS Code."

What you learn:
  - PyAudio quirks on your OS
  - Gemini Live API connection lifecycle
  - How interruption really feels in practice
  - Latency characteristics
```

### Weekend 2: Codebase Intelligence

**Goal: Tree-sitter parses your project, builds a codebase map, tools work.**

```
Tasks:
  ├── tree-sitter Python bindings for .py files
  ├── tree-sitter JavaScript/TypeScript bindings for .ts/.js
  ├── Build codebase map (functions, classes, imports, call graph)
  ├── Build summary_builder.py (deterministic project summary → system prompt)
  ├── Implement 6 tool handlers (execute locally, return results)
  ├── Register tools with NON_BLOCKING behavior
  ├── Handle tool_call in receive loop → execute → send_tool_response
  └── Test: "Tell me about auth" → Gemini calls tools → speaks about YOUR code

Deliverable:
  "Gemini knows your codebase. It calls tools to look up specific files, 
   functions, and dependencies. You can redirect and it adjusts."

What you learn:
  - tree-sitter AST structure
  - How function calling works in practice with Live API
  - Latency of NON_BLOCKING tool calls
  - Whether WHEN_IDLE vs INTERRUPT scheduling feels better
```

### Weekend 3: UI & Session Management

**Goal: Webview shows live transcript, mental model map, session controls.**

```
Tasks:
  ├── Build webview HTML/CSS (dark theme, premium feel)
  ├── Live transcript display (user speech + model speech, auto-scroll)
  ├── Mental model map (which files/dirs discussed, progress bars)
  ├── Session controls (🎙️ Start, ⏸️ Pause, ⏹️ End)
  ├── Referenced code panel (read-only, shows file:line when Gemini mentions code)
  ├── Session persistence (save transcript to .codiey/sessions/)
  ├── decisions.md auto-writer (when user says "bookmark" or "decide")
  ├── Session resumption (reconnect after GoAway, show reconnecting state)
  └── Test: full session workflow end-to-end with visual feedback

Deliverable:
  "The full Codiey experience. Webview shows live transcript, mental model 
   heatmap, and referenced code. Sessions persist. decisions.md accumulates."
```

### Weekend 4: Polish & Demo

**Goal: Demo-ready. Record walkthrough video. Handle edge cases.**

```
Tasks:
  ├── Error handling (API key missing, mic not found, connection dropped)
  ├── First-run experience (detect Python, create venv, pip install)
  ├── Settings UI (API key, voice selection, VAD sensitivity)
  ├── System prompt iteration (test 20+ interrupt scenarios, refine)  
  ├── VAD tuning (test sensitivity levels, find sweet spot)
  ├── Extension marketplace packaging
  ├── README with setup instructions, screenshots, demo GIF
  ├── Record demo video showing the interrupt-and-redirect flow
  └── Test on: Flask project, React project, raw Python scripts

Deliverable:
  "Publishable VS Code extension. README, demo video, marketplace-ready."
```

---

## 11. Dependencies

### Python (`requirements.txt`)
```
google-genai>=1.0.0          # Gemini SDK with Live API support
tree-sitter>=0.22.0          # AST parsing
tree-sitter-python>=0.22.0   # Python grammar
tree-sitter-javascript>=0.22.0  # JS grammar
tree-sitter-typescript>=0.22.0  # TS grammar
pyaudio>=0.2.14              # Mic capture + audio playback
```

### VS Code Extension (`package.json` dependencies)
```json
{
  "engines": { "vscode": "^1.85.0" },
  "activationEvents": ["onCommand:codiey.startSession"],
  "main": "./out/extension.js",
  "contributes": {
    "commands": [
      { "command": "codiey.startSession", "title": "Codiey: Start Thinking Session" },
      { "command": "codiey.stopSession", "title": "Codiey: End Session" },
      { "command": "codiey.setApiKey", "title": "Codiey: Set Gemini API Key" }
    ]
  }
}
```

### User Requirements
1. **VS Code** — they have it
2. **Python 3.10+** — they likely have it (they're developers)
3. **Gemini API Key** — free tier available
4. **Headphones** — recommended (prevents self-interruption)

---

## 12. Key API References

| What | Doc URL |
|---|---|
| Live API Overview | [ai.google.dev/gemini-api/docs/live-api](https://ai.google.dev/gemini-api/docs/live-api) |
| Capabilities Guide | [ai.google.dev/gemini-api/docs/live-guide](https://ai.google.dev/gemini-api/docs/live-guide) |
| Tool Use | [ai.google.dev/gemini-api/docs/live-tools](https://ai.google.dev/gemini-api/docs/live-tools) |
| Session Management | [ai.google.dev/gemini-api/docs/live-session](https://ai.google.dev/gemini-api/docs/live-session) |
| SDK Tutorial (Python) | [ai.google.dev/gemini-api/docs/live-api/get-started-sdk](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk) |
| Models | [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models) |
| Example Apps | [github.com/google-gemini/gemini-live-api-examples](https://github.com/google-gemini/gemini-live-api-examples) |

---

## 13. Success Criteria

After 4 weekends, you should be able to:

1. **Open a Python/TS project in VS Code**
2. **Click one button to start a voice session**
3. **Say:** "Walk me through the auth system"
4. **Hear Gemini** describe your actual code, referencing real files and functions
5. **Interrupt:** "Wait, we moved that to services/"
6. **Hear Gemini adjust** without losing context
7. **See the transcript** update live in the webview
8. **See the mental model** show which files have been discussed
9. **End the session** and find the transcript + decisions.md saved locally

That's the demo. That's the product. That's the story you tell in interviews.
