"""FastAPI backend — serves UI, mints ephemeral tokens, executes tools.

Architecture: No pre-built codebase map. The workspace path is stored at
startup, and all code intelligence is computed on-demand by tool handlers.
"""

import asyncio
import datetime
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("codiey")

app = FastAPI(title="Codiey", version="0.3.0")

# ── Static files ──
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Workspace state (populated at startup) ──
_workspace_path: Path | None = None
_repo_map = None


def get_repo_map():
    global _repo_map
    if _repo_map is None:
        from codiey.codebase.repo_map import RepoMap
        _repo_map = RepoMap(_workspace_path)
        if not _repo_map.load_cache():
            _repo_map.build()
    return _repo_map


@app.on_event("startup")
async def configure_workspace():
    """Store the workspace path at server startup. No heavy parsing."""
    global _workspace_path
    workspace = os.environ.get("CODIEY_WORKSPACE", os.getcwd())
    _workspace_path = Path(workspace)
    logger.info(f"Workspace configured: {_workspace_path}")


def _get_workspace() -> Path:
    """Get the configured workspace path."""
    if _workspace_path is None:
        raise HTTPException(status_code=503, detail="Workspace not configured")
    return _workspace_path


# ── Routes ──


@app.get("/")
async def serve_index():
    """Serve the main UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/key")
async def get_api_key():
    """Return the API key for direct WebSocket connection.

    This is safe because Codiey runs locally — the key never leaves localhost.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
    return {"key": api_key}


@app.get("/api/token")
async def get_ephemeral_token():
    """Mint a short-lived ephemeral token for browser → Gemini direct connection."""

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")

    try:
        from google import genai

        client = genai.Client(
            api_key=api_key, http_options={"api_version": "v1alpha"}
        )

        now = datetime.datetime.now(tz=datetime.timezone.utc)

        token = client.auth_tokens.create(
            config={
                "uses": 1,
                "expire_time": (now + datetime.timedelta(minutes=30)).isoformat(),
                "new_session_expire_time": (
                    now + datetime.timedelta(minutes=2)
                ).isoformat(),
                "http_options": {"api_version": "v1alpha"},
            }
        )

        return {
            "token": token.name,
            "expires_at": (now + datetime.timedelta(minutes=30)).isoformat(),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token minting failed: {str(e)}")


# ── Tool Execution ──

# Tier 2 tools: side-effects only. The endpoint returns {"status":"queued"}
# immediately and runs the handler in a thread pool so the voice stream
# never blocks on them. The frontend sends this response back to Gemini
# with SILENT scheduling so Gemini doesn't acknowledge the call verbally.
TIER_2_TOOLS = {"mark_as_discussed", "write_to_rules"}

# Track in-flight Tier 2 futures so /api/session/end can drain them
_pending_tier2: set[asyncio.Future] = set()


class ToolRequest(BaseModel):
    tool_name: str
    args: dict = {}


@app.post("/api/tools/execute")
async def execute_tool_endpoint(request: ToolRequest):
    """Execute a tool call from Gemini and return the result."""
    workspace = _get_workspace()

    from codiey.tools.handlers import execute_tool

    if request.tool_name in TIER_2_TOOLS:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, execute_tool, request.tool_name, request.args, workspace)
        _pending_tier2.add(future)
        future.add_done_callback(_pending_tier2.discard)
        return {"status": "queued"}

    result = execute_tool(request.tool_name, request.args, workspace)
    return result


# ── Session Lifecycle ──


@app.post("/api/session/start")
async def session_start():
    """Signal that a new session is beginning. Resets the in-memory mental model."""
    global _repo_map
    _repo_map = None  # Force a fresh build/cache-load for new sessions
    from codiey.tools.handlers import reset_mental_model
    reset_mental_model()
    return {"status": "ok"}


@app.post("/api/session/end")
async def session_end():
    """Signal that a session is ending. Drains pending Tier 2 background tasks
    (2-second timeout) then persists the mental model to disk."""
    workspace = _workspace_path

    if _pending_tier2:
        try:
            done, pending = await asyncio.wait(
                list(_pending_tier2), timeout=2.0,
            )
            if pending:
                logger.warning(f"session/end: Tier 2 drain timed out — {len(pending)} task(s) still running")
        except Exception as e:
            logger.warning(f"session/end: Tier 2 drain error — {e}")

    if workspace:
        from codiey.tools.handlers import get_mental_model
        import json

        model = get_mental_model()
        codiey_dir = workspace / ".codiey"
        codiey_dir.mkdir(exist_ok=True)
        model_path = codiey_dir / "mental-model.json"
        try:
            with open(model_path, "w", encoding="utf-8") as f:
                json.dump(model, f, indent=2)
        except OSError as e:
            logger.warning(f"session/end: mental model save failed: {e}")

    return {"status": "ok", "pending_drained": len(_pending_tier2) == 0}


@app.get("/api/tools/declarations")
async def get_tool_declarations():
    """Return the tool declarations for the Gemini setup message."""
    from codiey.tools.declarations import TOOL_DECLARATIONS
    return TOOL_DECLARATIONS


@app.get("/api/workspace/summary")
async def get_workspace_summary():
    """Return the lightweight project summary for system prompt injection."""
    workspace = _get_workspace()

    from codiey.codebase.summary_builder import build_lightweight_summary
    summary = build_lightweight_summary(workspace)

    return {
        "summary": summary,
        "project_name": workspace.name,
    }


@app.get("/api/mental-model")
async def get_mental_model():
    """Get the current mental model state."""
    from codiey.tools.handlers import get_mental_model
    return get_mental_model()


# ── Session Logging ──


@app.post("/api/session-log")
async def append_session_log(request: dict):
    """Append session log entries to a JSONL file.

    Each entry is written as a separate line for crash-resilience.
    Logs are stored in .codiey/session_logs/{sessionId}.jsonl
    """
    import json

    workspace = _get_workspace()
    log_dir = workspace / ".codiey" / "session_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    session_id = request.get("sessionId", "unknown")
    entries = request.get("entries", [])

    log_file = log_dir / f"{session_id}.jsonl"

    with open(log_file, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, default=str) + "\n")

    return {"status": "ok", "entries_written": len(entries)}


# ── Existing Endpoints ──


@app.post("/api/traces")
async def save_traces(request: dict):
    """Save debug traces to .codiey/traces.json for analysis."""
    import json

    workspace = _get_workspace()
    codiey_dir = workspace / ".codiey"
    codiey_dir.mkdir(exist_ok=True)

    traces_file = codiey_dir / "traces.json"
    with open(traces_file, "w", encoding="utf-8") as f:
        json.dump(request, f, indent=2)

    return {"status": "ok", "path": str(traces_file)}


@app.get("/api/health")
async def health():
    """Health check."""
    workspace = _workspace_path
    return {
        "status": "ok",
        "workspace": str(workspace) if workspace else "not set",
        "workspace_configured": workspace is not None,
    }
