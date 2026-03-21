# Codiey — system overview

## What it is

**Codiey** is a voice-first assistant that reasons over a local codebase: microphone → Gemini Live (native audio) → tools (grep, read file, graph) → spoken + visual response (graph + transcript).

## Main components

| Layer | Location | Role |
|-------|----------|------|
| **Backend** | [`codiey/app.py`](../../codiey/app.py) | FastAPI: static UI, workspace APIs, tool execution, session, mental model |
| **CLI** | [`codiey/cli.py`](../../codiey/cli.py) | `codiey start` — loads `.env`, sets `CODIEY_WORKSPACE`, runs Uvicorn |
| **Browser app** | [`codiey/static/`](../../codiey/static/) | `app.js` (WebSocket, VAD, graph D3), `styles.css`, `index.html` |
| **Code intelligence** | [`codiey/codebase/`](../../codiey/codebase/) | Tree-sitter parse, chunks, repo map, PageRank graph |
| **Tools** | [`codiey/tools/`](../../codiey/tools/) | Gemini function declarations + handlers |

## Runtime data

Session/workspace cache lives under **`.codiey/`** (gitignored): maps, logs, mental model JSON.

## Related docs

- Plans: [`../plans/`](../plans/)
- Past debugging notes: [`../archive/debug-notes/`](../archive/debug-notes/)
- ADRs: [`../adr/`](../adr/)
