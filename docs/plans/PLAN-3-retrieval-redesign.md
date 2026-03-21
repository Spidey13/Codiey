# Plan 3: Tree Traversal Retrieval System

> Priority: MEDIUM — dramatically improves retrieval quality, tool results go from 400 tokens to 20-50 tokens
> Files: new `codiey/codebase/repo_map.py`, modified `codiey/tools/handlers.py`, modified `codiey/tools/declarations.py`, `pyproject.toml`
> New dependency: `networkx`

---

## Current State

- `read_file` dumps entire file chunks using AST-boundary chunking (~400 tokens per call)
- `get_function_info` finds a function by name, extracts callees from the function body via tree-sitter, finds callers via grep across all files
- `grep_search` does regex/text search across all files — this stays as-is
- No awareness of which files or symbols are most important to the current conversation
- No ranked retrieval — every file is treated equally
- Tool results fill the context window quickly, get pruned by SlidingWindow, losing potentially important information

---

## Architecture Overview

Two layers working together:

```
Layer 1: RANKING (background, cached)                Layer 2: RETRIEVAL (on-demand, per tool call)
─────────────────────────────────                     ──────────────────────────────────────────────
Parse all files with tree-sitter                      grep_search finds entry point
Extract definitions + references                      Navigate UP to parent (class/module) for context
Build directed graph (file→file via symbols)          Navigate DOWN through call graph for details
Run PageRank with personalization                     Return compact node summaries (20-50 tokens)
Cache ranked results to disk                          Never dump whole files
Refresh incrementally on file change                  Rank-aware: prefer nodes in important files
```

---

## Phase 1: Build the RepoMap Module

**New file:** `codiey/codebase/repo_map.py`

This module builds and maintains a ranked symbol map of the workspace, inspired by [Aider's repomap](https://aider.chat/2023/10/22/repomap.html).

### Core Data Structures

```python
from collections import defaultdict, namedtuple
from pathlib import Path

Tag = namedtuple("Tag", ["rel_path", "abs_path", "name", "kind", "line"])
# kind: "def" (definition) or "ref" (reference)
```

### Key Functions

**`extract_tags(file_path: Path, rel_path: str) -> list[Tag]`**

Uses tree-sitter (already available via `codiey/codebase/parser.py`) to extract:
- **Definitions:** function declarations, class declarations, method definitions, variable assignments at module scope
- **References:** identifiers used in call expressions, imports, type annotations

For each file, parse with tree-sitter and walk the AST:
- Nodes of type `function_definition`, `class_definition` → Tag(kind="def", name=identifier, line=start_line)
- Nodes of type `call` / `call_expression` → extract the function name → Tag(kind="ref", name=callee_name, line=start_line)
- Import statements → Tag(kind="ref", name=imported_name, line=start_line)

Use the existing `parse_file()` from `codiey/codebase/parser.py` which already handles Python, JavaScript, and TypeScript via tree-sitter.

**`build_graph(workspace: Path) -> nx.MultiDiGraph`**

1. Walk all source files in workspace (reuse `SKIP_DIRS` and gitignore logic from `codiey/codebase/workspace.py`)
2. For each file, call `extract_tags()` to get definitions and references
3. Build two indexes:
   - `defines[symbol_name] → set of files that define it`
   - `references[symbol_name] → list of files that reference it`
4. Create a `networkx.MultiDiGraph`:
   - For each symbol that is both defined and referenced:
   - For each (referencer_file, definer_file) pair:
   - Add edge with weight = multiplier based on:
     - Is the symbol a meaningful name? (camelCase, snake_case, length >= 8) → weight × 10
     - Is it private? (starts with `_`) → weight × 0.1
     - Is it defined in many files? (> 5) → weight × 0.1 (too generic)
     - Number of references: `sqrt(count)` (diminishing returns)

**`get_ranked_files(graph, chat_files=None, mentioned_symbols=None, token_budget=1024) -> list[tuple[str, float]]`**

1. Compute personalization dict: files currently being discussed get personalization boost (100/num_nodes)
2. Run `nx.pagerank(graph, weight="weight", personalization=personalization)`
3. Distribute rank from each node across its outgoing edges to rank individual definitions
4. Sort definitions by rank, return top definitions that fit within token_budget
5. Return as list of (rel_path, symbol_name, definition_line, rank_score)

**`class RepoMap`**

```python
class RepoMap:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.cache_path = workspace / ".codiey" / "repo_map_cache.json"
        self.graph = None
        self.tags_cache = {}  # file_path → (mtime, tags)
        self.ranked = None

    def build(self):
        """Full build — run once at workspace open."""
        # Parse all files, build graph, run PageRank
        # Cache to self.cache_path

    def load_cache(self) -> bool:
        """Load from disk cache. Return True if cache is valid."""

    def get_ranked_context(self, chat_files, mentioned_symbols, token_budget=1024) -> str:
        """Return ranked repo map content within token budget."""

    def invalidate_file(self, file_path: Path):
        """Incrementally update when a file changes."""
```

### Caching Strategy

- On first call: full build, save to `.codiey/repo_map_cache.json`
- On subsequent calls: load from cache, check mtimes of files, re-parse only changed files
- Cache format: `{ "version": 1, "files": { "rel_path": { "mtime": float, "tags": [...] } } }`
- Graph and PageRank recomputed when any file changes (fast — PageRank on a few hundred nodes is <100ms)

---

## Phase 2: Integrate RepoMap with Startup

**File:** `codiey/app.py`

Build the RepoMap lazily on first tool call, not at startup (avoid blocking session start).

Add a module-level variable:

```python
_repo_map: RepoMap | None = None

def get_repo_map() -> RepoMap:
    global _repo_map
    if _repo_map is None:
        _repo_map = RepoMap(_workspace_path)
        if not _repo_map.load_cache():
            _repo_map.build()
    return _repo_map
```

In `session_start` endpoint, reset the repo map so it rebuilds for fresh projects:

```python
@app.post("/api/session/start")
async def session_start():
    global _repo_map
    _repo_map = None
    reset_mental_model()
    return {"status": "ok"}
```

---

## Phase 3: Enhanced Read File with Ranked Context

**File:** `codiey/tools/handlers.py`

Modify `_handle_read_file` to use the RepoMap for smarter content selection.

### Current Behavior
- Reads file, uses AST chunking, returns first chunk + continuation hint
- Returns ~400 tokens of raw code

### New Behavior
- If called with just `file_path` (no start_line/end_line): return a **ranked summary** of the file
  - Use RepoMap to identify the most important definitions in this file
  - For each important definition: show signature + first line of docstring (if any)
  - Total output: 20-50 tokens for a typical file
  - Include a hint: "Use get_function_info('name') for details on any of these"
- If called with `start_line`/`end_line`: return raw content for that range (existing behavior, for when the model knows exactly what it wants)

```python
def _handle_read_file(args: dict, workspace: Path) -> dict:
    args.pop("reasoning", None)
    file_path = args.get("file_path", "").strip()
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    if start_line or end_line:
        # Specific range requested — use existing AST chunking behavior
        return _read_file_range(file_path, start_line, end_line, workspace)

    # No range specified — return ranked summary
    repo_map = get_repo_map()
    ranked_defs = repo_map.get_file_summary(file_path)

    if not ranked_defs:
        # Fallback to existing behavior if file not in map
        return _read_file_chunked(file_path, workspace)

    summary_lines = [f"# {file_path} — key definitions:"]
    for defn in ranked_defs:
        summary_lines.append(f"  {defn.kind} {defn.name} (line {defn.line})")
    summary_lines.append(f"\nUse get_function_info('name') for full details.")

    return {
        "file": file_path,
        "summary": "\n".join(summary_lines),
        "hint": "Call get_function_info for any symbol above, or read_file with start_line/end_line for raw content"
    }
```

---

## Phase 4: Enhanced get_function_info with Tree Traversal

**File:** `codiey/tools/handlers.py`

Modify `_handle_get_function_info` to do AST tree traversal — navigating UP for context and DOWN through call graph.

### Current Behavior
- Finds function definition using `parse_file()`
- Extracts callees by re-parsing with tree-sitter and finding call expressions in function body
- Finds callers via grep across all source files
- Returns: name, file, line, params, return_type, docstring, callees list, callers list

### Enhanced Behavior

Keep the existing approach but make the output more compact and add context navigation:

**Navigate UP:** When the function is a method inside a class, include the class name and a one-line description of the class (first line of class docstring or just the class signature).

**Navigate DOWN (callees):** For each callee that's defined in the workspace (not stdlib/external):
- Include which file it's defined in
- Include its signature (one line)
- Rank callees by PageRank importance — show top 5 only

**Callers (keep grep-based):** Show top 5 callers ranked by PageRank importance, not all of them.

**Output format — compact:**

```
_handle_read_file in codiey/tools/handlers.py:244
  class: (module-level function)
  params: args: dict, workspace: Path
  returns: dict
  callees (top 5):
    parse_file (codiey/codebase/parser.py:45)
    get_repo_map (codiey/app.py:30)
    _read_file_range (codiey/tools/handlers.py:280)
  callers (top 5):
    execute_tool (codiey/tools/handlers.py:15)
```

This gives the model everything it needs to navigate further in ~50 tokens instead of 400.

---

## Phase 5: Expose Ranked Context in Summary Builder

**File:** `codiey/codebase/summary_builder.py`

Optionally include a brief ranked file list in the codebase summary injected into the system instruction.

After the directory tree section, add:

```
## Key Files (by importance)
1. codiey/static/app.js — frontend logic
2. codiey/tools/handlers.py — tool execution
3. codiey/app.py — backend API
...
```

This gives Gemini a starting point for which files to investigate first, based on the PageRank analysis.

Only include top 10 files. The rest are discoverable via tools.

---

## Phase 6: Add networkx Dependency

**File:** `pyproject.toml`

Add `networkx` to the dependencies:

```toml
dependencies = [
    # ... existing deps
    "networkx>=3.0",
]
```

Run `uv sync` or `pip install networkx` to install.

---

## Tool Declaration Changes

**File:** `codiey/tools/declarations.py`

No changes to tool names or schemas. The tools stay the same:
- `grep_search` — unchanged
- `file_search` — unchanged
- `list_directory` — unchanged
- `read_file` — same interface, smarter output
- `get_function_info` — same interface, more compact + ranked output
- `mark_as_discussed` — unchanged (but the discussed files feed into PageRank personalization)
- `write_to_rules` — unchanged

The improvement is all in the handler implementations, not the tool interface. This means no changes to the Gemini tool declarations and no risk of schema-related crashes.

---

## How It All Connects

```
User asks: "What does the frontend use for the backend?"

1. Gemini calls grep_search(query="fetch|api|backend", include="js,ts,tsx")
   → Returns: 3 matches in frontend/src/api/client.ts (lines 12, 45, 78)
   → Cost: ~30 tokens

2. Gemini calls read_file(file_path="frontend/src/api/client.ts")
   → Returns ranked summary: key definitions (ApiClient class, fetchData, BASE_URL)
   → Cost: ~25 tokens

3. Gemini calls get_function_info(function_name="fetchData")
   → Returns: signature, class context, top callees (axios.get), top callers (useProjects, useSkills)
   → Cost: ~40 tokens

Total context cost: ~95 tokens (vs ~1200 tokens with current read_file dumping full chunks)
Gemini answers accurately with 3 targeted tool calls.
```

---

## Implementation Order

1. Create `codiey/codebase/repo_map.py` with `extract_tags()` and `build_graph()` — test with existing parser
2. Add PageRank ranking with `get_ranked_files()`
3. Add caching to disk
4. Integrate with `_handle_read_file` (ranked summary mode)
5. Enhance `_handle_get_function_info` (compact output, ranked callees/callers)
6. Add to summary builder (top 10 files)
7. Add `networkx` dependency

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| tree-sitter tag extraction misses definitions in some languages | Fallback to pygments tokenizer for references (Aider does this) |
| PageRank too slow for large repos | Cache aggressively, only recompute changed files, lazy build on first tool call |
| Ranked summary too terse for model to understand | Keep `start_line`/`end_line` mode as escape hatch for raw content |
| networkx adds dependency weight | It's a pure Python package, no native deps, ~1.5MB |
| Model calls read_file expecting full content, gets summary | The summary includes a hint to use get_function_info or start_line/end_line for details |
