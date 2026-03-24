# Codiey

> Voice-first codebase thinking partner — you talk, it reasons over your repo in real time.

Codiey connects your microphone directly to **Gemini 2.5 Flash native audio** over a live WebSocket, gives the model a suite of code-intelligence tools (grep, read file, AST introspection, directory listing), and visualises which files it's reasoning about on an interactive **PageRank dependency graph** — all inside a single-page browser app served by a tiny FastAPI backend.

Installed as a single Python package. No separate frontend build. Point it at any local repo and start talking.

<video src="assets/Codiey.mp4" controls="controls" style="max-width: 100%;">
  Your browser does not support the video tag.
</video>

---

## What makes it technically distinct

### 1. End-to-end real-time audio (not REST + MediaRecorder)

- **Input:** AudioWorklet (`pcm-processor.js`) captures microphone at 16 kHz, converts Float32 → Int16 PCM, and posts raw buffers to the main thread at worklet cadence.
- **Output:** 24 kHz PCM streamed back from the model is decoded and played via Web Audio API with no intermediate re-encoding.
- **Protocol:** `BidiGenerateContent` (`v1alpha`) over a raw WebSocket — not the constrained Live path, because **function calling only works on the Bidi path**.

### 2. Neural VAD voice gate (`@ricky0123/vad-web`)

A client-side ONNX neural network (Silero VAD) runs in the browser and controls exactly when PCM bytes are forwarded to Gemini. This replaces an older energy-threshold approach that caused mid-utterance truncation.

- `onSpeechStart` → `state.isUserSpeaking = true` → audio flows.
- `onSpeechEnd` → `state.isUserSpeaking = false` → audio stops instantly.
- Layered with a hard `TOOL_PENDING` gate — **no audio ever leaks during tool execution**, which was the root cause of `1008` WebSocket crashes. See [ADR 0002](docs/adr/0002-neural-vad-voice-gating.md) and [ADR 0003](docs/adr/0003-audio-state-machine-tool-gate.md).

### 3. Two-tier tool architecture with `SILENT` scheduling

Tools are split into two tiers so the voice stream never blocks on side-effects:

| Tier | Tools | Return | Audio impact |
|------|-------|--------|--------------|
| **1 — Model must read result** | `read_file`, `grep_search`, `file_search`, `list_directory`, `get_function_info` | Full result | `TOOL_PENDING` gate active |
| **2 — Side-effect only** | `write_to_rules`, `mark_as_discussed` | `{"status":"queued"}` immediately | No gate; runs in thread pool |

Tier 2 responses are sent back to Gemini with **`scheduling: "SILENT"`** so the model doesn't narrate the write operation. The session-end route drains the thread pool before the backend exits.

### 4. Session resilience and auto-reconnect

- **Sliding window compression** (`contextWindowCompression`) — Gemini automatically prunes oldest turns when the context fills. The `systemInstruction` (rules file + directory tree) is always preserved.
- **Resumption tokens** — every session is assigned a `sessionResumptionUpdate` handle stored in `localStorage`. On any unexpected WebSocket close (network drop, 1008, 1011), the client silently reconnects and presents the handle — conversation context is restored without user action.
- **`goAway` handling** — the server signals ~60 seconds before a forced cycle. Codiey proactively reconnects in the background; the user sees nothing.
- **Audio survives reconnects** — the `AudioContext` and mic stream are kept alive across reconnections (`endSession(keepAudio=true)`) per Gemini Live API guidance.

See [ADR 0004](docs/adr/0004-session-resumption.md).

### 5. Forced model reasoning on every tool call

Every tool declaration includes a **required `reasoning` string** parameter. The backend ignores its value; it exists to force the model to emit a structured internal thought before touching your repository files, reducing hallucinated tool arguments.

### 6. Real code intelligence (not file-path guessing)

- **Tree-sitter** parses Python, JavaScript, and TypeScript into ASTs.
- A **directed dependency graph** is built from import/require edges across the codebase.
- Files are ranked with **PageRank** (NetworkX + personalization vector) — the model's tool calls can bias the walk toward recently-touched files.
- The live UI shows the top-ranked slice as a **D3 force-directed graph**: high-PageRank nodes sit at the centre and glow; traversal pulses animate along edges as the model reads files.

### 7. Persistent session memory (`write_to_rules`)

As the model learns stable facts about your codebase or working style during a session, it calls `write_to_rules` with a single-sentence insight. These are appended to `.codiey/rules` and injected at the top of the system instruction on the next session — giving Codiey a growing per-project memory that survives model context resets.

### 8. Live dual transcription

Both sides of every conversation are transcribed in real time:

- **Input transcription** (`inputAudioTranscription`) — what you said, streamed into the chat panel as you speak.
- **Output transcription** (`outputAudioTranscription`) — what the model said, streamed word-by-word alongside the audio.

---

## Stack

| Area | Choice |
|------|--------|
| Runtime | Python 3.10+ |
| Server | FastAPI + Uvicorn |
| Model | Gemini 2.5 Flash **native audio** (Live / Bidi) |
| Client VAD | `@ricky0123/vad-web` + ONNX Runtime (in-browser) |
| Client graph | D3.js force simulation |
| Graph math | NetworkX, NumPy, SciPy (PageRank) |
| Parsing | tree-sitter + Python / JS / TS grammars |
| Package / env | **[uv](https://github.com/astral-sh/uv)** recommended |

---

## Quick start

### Prerequisites

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (recommended) or a classic venv + pip
- Python **3.10+**
- A **Gemini API key** ([Google AI Studio](https://aistudio.google.com/apikey))
- **Headphones** recommended (echo cancellation is on, but speaker bleed is real)

### Install

```bash
git clone <your-repo-url>
cd <repo-directory>
uv sync
```

### Configure

```bash
cp .env.example .env
# Edit .env — set GEMINI_API_KEY=...
```

### Run

Point `--workspace` at the codebase you want to reason about:

```bash
uv run codiey start --workspace /path/to/your/project
```

Defaults: **http://127.0.0.1:7842**, browser opens automatically.

```bash
# Talk about Codiey's own source
uv run codiey start --workspace .

# Different project, different port
uv run codiey start --workspace /path/to/other/repo --port 8000

# No auto-browser
uv run codiey start --workspace . --no-browser
```

### Without uv

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
codiey start --workspace /path/to/project
```

---

## Project layout

```
codiey/
├── app.py              # FastAPI: static UI, session API, tools, graph API
├── cli.py              # `codiey start` CLI entry point
├── static/             # index.html, app.js, styles.css, pcm-processor.js
├── tools/              # Tool declarations (Gemini schemas) + handlers
└── codebase/           # Tree-sitter parser, repo map, PageRank, summaries

docs/
├── architecture/       # System overview
├── adr/                # Architecture Decision Records
├── plans/              # Design plans and roadmaps
└── archive/            # Historical debug notes

.codiey/                # Runtime cache — gitignored
                        # (session logs, mental model, repo map cache)
```

---

## How a session works

1. **CLI** sets `CODIEY_WORKSPACE` env var and starts Uvicorn.
2. **Browser** fetches tool declarations, codebase summary (rules + directory tree), and an ephemeral API key, then opens a WebSocket **directly** to Gemini's `BidiGenerateContent` endpoint.
3. **Setup message** configures native audio, VAD sensitivity, both transcription channels, context compression, and a resumption handle — all in a single JSON frame.
4. **Mic → Gemini:** VAD gate fires, AudioWorklet posts PCM → `sendAudioChunk` encodes to base64 and sends via WebSocket.
5. **Gemini → Mic:** Model streams audio chunks back; `AudioPlayer` queues and plays them. Transcription tokens arrive on the same socket and update the chat panel.
6. **Tool calls:** Model emits a `functionCall` stub → client enters `TOOL_PENDING` → POSTs to `/api/tools/execute` → sends `toolResponse` back → model continues.
7. **Graph:** Tool activity dynamically adds nodes and pulses edges in the D3 graph to show which files the model is currently anchored on.

---

## Security note

The API key route and direct browser session are designed for **localhost use only**. Do not expose this server to the internet without redesigning auth and key handling.

---

## Docs & ADRs

| Resource | Path |
|----------|------|
| Docs index | [`docs/README.md`](docs/README.md) |
| System architecture | [`docs/architecture/overview.md`](docs/architecture/overview.md) |
| ADRs | [`docs/adr/`](docs/adr/) |
| Design plans | [`docs/plans/`](docs/plans/) |
| Debug archive | [`docs/archive/`](docs/archive/) |

Key ADRs:
- [0002 — Neural VAD voice gating](docs/adr/0002-neural-vad-voice-gating.md)
- [0003 — Audio state machine / 1008 fix](docs/adr/0003-audio-state-machine-tool-gate.md)
- [0004 — Session resumption & context compression](docs/adr/0004-session-resumption.md)

---

## License

MIT — see [`pyproject.toml`](pyproject.toml).

---

## Dev scripts

Smoke tests for parser and retrieval (run from repository root):

```bash
python scripts/dev/test_parser_quick.py
```
