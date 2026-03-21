# Codiey

Voice-first assistant for a **local** codebase. You talk; it pulls context with tools (grep, read file, AST introspection), answers in **native audio** over Gemini Live, and keeps a **live dependency graph** in the UI so you can see what file the model is anchored on.

Built as a single installable Python package with a vanilla JS front end—no separate frontend build step.

---

## Why it’s not “just a chatbot wrapper”

- **End-to-end audio path** — Mic capture runs through the Web Audio **AudioWorklet** at 16 kHz PCM; playback is 24 kHz PCM from the model. That’s not `MediaRecorder` + REST; it’s the same constraints as a real-time voice product.
- **Browser ↔ Gemini over WebSocket** — The app uses **BidiGenerateContent** (`v1alpha`) because **function calling doesn’t work on the constrained Live path**. The backend mints a **short-lived auth token** (`/api/token`) so the session isn’t proxying every audio frame through Python.
- **Tool latency is designed in** — Tools split into **Tier 1** (model must see the result: `read_file`, `grep_search`, etc.) and **Tier 2** (side effects only: session memory, rules). Tier 2 returns immediately (`{"status":"queued"}`) and runs in a **thread pool** so the voice stream never blocks; the client sends the response back with **SILENT** scheduling so the model doesn’t narrate “I’m writing to disk.” Session end **drains** those futures before persisting state.
- **Forced reasoning on every tool call** — Every declaration includes a required `reasoning` string. The backend ignores it; it exists to structure what the model emits before it touches your repo.
- **Code intelligence is real** — Parsing is **Tree-sitter** (Python / JS / TS grammars). The repo map is a **directed graph** with **PageRank** (NetworkX + personalization) to rank files; the UI shows the top slice as nodes/edges, not a static file tree.

Those pieces are wired together in [`codiey/app.py`](codiey/app.py), [`codiey/static/app.js`](codiey/static/app.js), [`codiey/tools/declarations.py`](codiey/tools/declarations.py), and [`codiey/codebase/repo_map.py`](codiey/codebase/repo_map.py).

---

## Stack

| Area | Choice |
|------|--------|
| Runtime | Python 3.10+ |
| Server | FastAPI + Uvicorn |
| Model | Gemini 2.5 Flash **native audio** (Live / Bidi) — see `GEMINI_MODEL` in `app.js` |
| Client | HTML/CSS + D3 for the graph; **ONNX + vad-web** for client-side VAD |
| Graph math | NetworkX, NumPy, SciPy (PageRank) |
| Parsing | tree-sitter + language bindings |
| Tooling | **[uv](https://github.com/astral-sh/uv)** recommended for install + `uv run` (no manual venv dance) |

---

## Quick start (replicate on your machine)

### 1. Prerequisites

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (recommended) or another way to install the package editable (see “Without uv” below)
- Python **3.10+** (uv will respect `requires-python` in `pyproject.toml`)
- A **Gemini API key** ([Google AI Studio](https://aistudio.google.com/apikey))
- **Headphones** recommended (echo cancellation helps, but full-duplex voice is picky)

### 2. Clone & sync

```bash
git clone <your-repo-url>
cd <repo-directory>   # root that contains pyproject.toml

uv sync
```

That creates a project environment and installs **Codiey** in editable mode so the `codiey` CLI is available. (You can also go straight to `uv run codiey start …`—uv will sync on first run if needed.)

### 3. Configure

Put your key in `.env` at the repo root (the CLI loads it via `python-dotenv`):

```bash
cp .env.example .env
# Edit .env — set GEMINI_API_KEY=...
```

### 4. Run

Point `--workspace` at the **codebase you want to talk about** (any directory on disk):

```bash
uv run codiey start --workspace D:\Projects\Codiey
```

Examples:

```bash
# Same folder as the clone (index Codiey’s own source)
uv run codiey start --workspace .

# Another project
uv run codiey start --workspace /path/to/other/repo --port 7842
```

Defaults: **http://127.0.0.1:7842**, browser opens automatically (`--no-browser` to disable).

### Without uv

If you prefer a classic venv:

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
├── codiey/                 # Installable package
│   ├── app.py              # FastAPI: UI, tokens, tools, session, graph API
│   ├── cli.py              # `codiey start`
│   ├── static/             # index.html, app.js, styles.css, worklet
│   ├── tools/              # Tool schemas + handlers
│   └── codebase/           # Parser, chunks, repo map, summaries
├── docs/                   # Architecture notes, ADRs, plans, archives
├── scripts/dev/            # Optional smoke scripts (run from repo root)
├── pyproject.toml
├── .env.example
└── README.md
```

Runtime artifacts (ignored by git) live under **`.codiey/`** inside the workspace: cache, mental model, session logs.

---

## Architecture (short)

1. **CLI** sets `CODIEY_WORKSPACE` and starts Uvicorn.
2. **Startup** only records the workspace path—no full-repo parse at import.
3. **Session start** resets in-memory mental model and can refresh repo map usage.
4. **Client** loads tool declarations + a **lightweight text summary** of the project for the system prompt, fetches key/token, opens a **WebSocket** to Gemini, streams audio both ways, and POSTs tool calls to **`/api/tools/execute`**.
5. **Graph** — `GET /api/workspace/graph` returns top-ranked files and edges for D3; tool activity can highlight nodes and edges as the conversation moves through files.

More detail: [`docs/architecture/overview.md`](docs/architecture/overview.md).

---

## Security note (intentional scope)

The API key route and direct browser session are **meant for localhost**. Don’t expose this server to the internet without redesigning auth and key handling. For demos, run locally or use a trusted network with full understanding of the risk.

---

## Docs & ADRs

- **Index:** [`docs/README.md`](docs/README.md)  
- **ADRs:** [`docs/adr/`](docs/adr/)  
- **Plans / history:** [`docs/plans/`](docs/plans/), [`docs/archive/`](docs/archive/)

---

## License

MIT — see [`pyproject.toml`](pyproject.toml).

---

## Optional dev scripts

Smoke tests for parser / retrieval (must run from **repository root** so `codiey/` paths resolve):

```bash
python scripts/dev/test_parser_quick.py
```

TypeScript parser experiments write under `scripts/dev/_scratch/` (gitignored).
