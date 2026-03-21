"""Tool handler dispatch — execute tools locally and return results.

All handlers work on-demand: they parse files as needed using tree-sitter,
rather than querying a pre-built codebase map.

Tier 1 (awaited — Gemini needs the result):
    grep_search, file_search, list_directory, read_file, get_function_info

Tier 2 (fire-and-forget — app.py runs these via run_in_executor and returns
        {"status": "queued"} before they finish):
    mark_as_discussed, write_to_rules
"""

from __future__ import annotations

import fnmatch
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


# Serialise concurrent writes to the rules file (Tier 2 runs in thread pool)
_rules_lock = threading.Lock()


# ── Mental model state (in-memory, per session) ──
_mental_model: dict[str, dict[str, Any]] = {}


def execute_tool(
    tool_name: str,
    args: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    """Dispatch a tool call to the appropriate handler."""
    handlers = {
        # Tier 1
        "grep_search": _handle_grep_search,
        "file_search": _handle_file_search,
        "list_directory": _handle_list_directory,
        "read_file": _handle_read_file,
        "get_function_info": _handle_get_function_info,
        # Tier 2
        "mark_as_discussed": _handle_mark_as_discussed,
        "write_to_rules": _handle_write_to_rules,
    }

    handler = handlers.get(tool_name)
    if not handler:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        return handler(args, workspace)
    except Exception as e:
        return {"error": f"Tool execution failed: {str(e)}"}


def get_mental_model() -> dict[str, dict[str, Any]]:
    """Get the current mental model state."""
    return _mental_model.copy()


def reset_mental_model():
    """Reset the mental model (e.g., on new session)."""
    global _mental_model
    _mental_model = {}


# ══════════════════════════════════════════════════════════════
# Tier 1 Handlers
# ══════════════════════════════════════════════════════════════


def _handle_grep_search(args: dict, workspace: Path) -> dict:
    """Search for text patterns across workspace files."""
    from codiey.codebase.workspace import walk_all_files

    args.pop("reasoning", None)
    query = args.get("query", "")
    use_regex = args.get("regex", False)
    include = args.get("include", "")

    if not query:
        return {"error": "query is required"}

    include_exts: set[str] = set()
    if include:
        for ext in include.split(","):
            ext = ext.strip().lower()
            if not ext.startswith("."):
                ext = "." + ext
            include_exts.add(ext)

    if use_regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}
    else:
        pattern = None

    results = []
    files_searched = 0
    max_results = 30

    for abs_path in walk_all_files(workspace):
        if include_exts and abs_path.suffix.lower() not in include_exts:
            continue

        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        files_searched += 1

        try:
            rel_path = str(abs_path.relative_to(workspace)).replace("\\", "/")
        except ValueError:
            continue

        for i, line in enumerate(content.split("\n"), 1):
            if use_regex and pattern:
                matched = bool(pattern.search(line))
            else:
                matched = query in line

            if matched:
                results.append({
                    "file": rel_path,
                    "line": i,
                    "content": line.strip()[:150],
                })
                if len(results) >= max_results:
                    return {
                        "query": query,
                        "results": results,
                        "total_matches": len(results),
                        "truncated": True,
                        "files_searched": files_searched,
                        "note": f"Showing first {max_results} matches. Narrow your search for more specific results.",
                    }

    return {
        "query": query,
        "results": results,
        "total_matches": len(results),
        "truncated": False,
        "files_searched": files_searched,
    }


def _handle_file_search(args: dict, workspace: Path) -> dict:
    """Find files matching a glob pattern across the workspace."""
    from codiey.codebase.workspace import walk_all_files

    args.pop("reasoning", None)
    pattern = args.get("pattern", "")
    dir_path = args.get("dir_path", "").strip()

    if not pattern:
        return {"error": "pattern is required"}

    matches = []
    max_matches = 50

    for abs_path in walk_all_files(workspace):
        if dir_path:
            try:
                rel = str(abs_path.relative_to(workspace)).replace("\\", "/")
                scoped = dir_path.rstrip("/") + "/"
                if not rel.startswith(scoped):
                    continue
            except ValueError:
                continue

        if fnmatch.fnmatch(abs_path.name, pattern):
            try:
                rel = str(abs_path.relative_to(workspace)).replace("\\", "/")
                matches.append(rel)
                if len(matches) >= max_matches:
                    break
            except ValueError:
                continue

    return {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": len(matches) >= max_matches,
    }


def _handle_list_directory(args: dict, workspace: Path) -> dict:
    """List immediate contents of a directory."""
    from codiey.codebase.workspace import SKIP_DIRS, load_gitignore_patterns, is_gitignored

    args.pop("reasoning", None)
    dir_path = args.get("dir_path", ".").strip() or "."

    abs_dir = workspace / dir_path
    if not abs_dir.exists():
        return {"error": f"Directory not found: {dir_path}"}
    if not abs_dir.is_dir():
        return {"error": f"Not a directory: {dir_path}"}

    gitignore = load_gitignore_patterns(workspace)
    entries = []

    try:
        for child in sorted(abs_dir.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if child.name.startswith("."):
                continue
            if child.name in SKIP_DIRS or child.name.endswith(".egg-info"):
                continue
            try:
                rel = child.relative_to(workspace)
            except ValueError:
                continue
            if is_gitignored(rel, gitignore):
                continue

            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
            })
    except PermissionError:
        return {"error": f"Permission denied: {dir_path}"}

    try:
        rel_dir = str(abs_dir.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        rel_dir = dir_path

    return {
        "path": rel_dir,
        "entries": entries,
        "count": len(entries),
    }


def _handle_read_file(args: dict, workspace: Path) -> dict:
    """Read file content using AST-boundary chunking or return a ranked summary."""
    from codiey.codebase.chunker import chunk_file
    from codiey.app import get_repo_map

    args.pop("reasoning", None)
    file_path = args.get("file_path", "").strip()
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    if start_line or end_line:
        # Specific range requested — use existing AST chunking behavior
        abs_path = _resolve_file_path(workspace, file_path)
        if abs_path is None:
            return {"error": f"File not found: {file_path}"}

        result = chunk_file(
            abs_path,
            token_budget=4000,
            start_line=start_line,
            end_line=end_line,
        )

        try:
            rel_path = abs_path.relative_to(workspace)
            result["file"] = str(rel_path).replace("\\", "/")
        except ValueError:
            result["file"] = file_path

        return result

    # No range specified — return ranked summary
    repo_map = get_repo_map()
    
    # Needs to match the internal rel_path formatting
    abs_path = _resolve_file_path(workspace, file_path)
    if abs_path is None:
        return {"error": f"File not found: {file_path}"}
        
    try:
        rel_path = str(abs_path.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        rel_path = file_path
        
    ranked_defs = repo_map.get_file_summary(rel_path)

    if not ranked_defs:
        # Fallback to existing behavior if file not in map or is empty
        result = chunk_file(abs_path, token_budget=4000)
        result["file"] = rel_path
        return result

    summary_lines = [f"# {rel_path} — key definitions:"]
    for defn in ranked_defs[:15]:  # limit to top 15 definitions to save context
        summary_lines.append(f"  {defn.kind} {defn.name} (line {defn.line})")
        
    if len(ranked_defs) > 15:
        summary_lines.append(f"  ... (+{len(ranked_defs) - 15} more)")

    return {
        "file": rel_path,
        "summary": "\n".join(summary_lines),
        "hint": "Call get_function_info for any symbol above, or read_file with start_line/end_line for raw content"
    }


def _handle_get_function_info(args: dict, workspace: Path) -> dict:
    """Find a function, extract its signature, callers (grep), and callees (AST body only)."""
    from codiey.codebase.parser import parse_file
    from codiey.codebase.workspace import walk_source_files

    args.pop("reasoning", None)
    function_name = args.get("function_name", "")
    file_path_hint = args.get("file_path", "")

    if not function_name:
        return {"error": "function_name is required"}

    # ── Locate the function definition ──
    target_file: Path | None = None
    target_fn: dict | None = None

    if file_path_hint:
        candidate = _resolve_file_path(workspace, file_path_hint)
        search_files = [candidate] if candidate else []
    else:
        search_files = list(walk_source_files(workspace))

    for abs_path in search_files:
        parsed = parse_file(abs_path)
        if not parsed:
            continue

        # Top-level functions
        for fn in parsed.get("functions", []):
            if fn["name"] == function_name:
                target_file = abs_path
                target_fn = dict(fn)
                break

        # Class methods
        if not target_fn:
            for cls in parsed.get("classes", []):
                for method in cls.get("methods", []):
                    if method["name"] == function_name:
                        target_file = abs_path
                        target_fn = dict(method)
                        target_fn["class"] = cls["name"]
                        break
                if target_fn:
                    break

        if target_fn:
            break

    if not target_fn or not target_file:
        return {"error": f"Function '{function_name}' not found"}

    try:
        rel_path = str(target_file.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        rel_path = str(target_file)

    # ── AST-based callees (scoped to function body only) ──
    callees = _extract_callees_from_body(
        target_file,
        function_name,
        target_fn["line"],
        target_fn.get("end_line", target_fn["line"]),
    )
    
    # Top 5 ranked callees
    from codiey.app import get_repo_map
    repo_map = get_repo_map()
    
    # Build a lookup to sort by global symbol rank
    rank_lookup = {}
    for f_path, rank in repo_map.ranked_files:
        for tag_dict in repo_map.tags_cache.get(f_path, {}).get("tags", []):
            if tag_dict["kind"] == "def":
                if tag_dict["name"] not in rank_lookup or rank > rank_lookup[tag_dict["name"]]:
                    rank_lookup[tag_dict["name"]] = rank
                    
    callees_ranked = sorted(callees, key=lambda c: rank_lookup.get(c, 0.0), reverse=True)[:5]
    if len(callees) > 5:
        callees_ranked.append(f"... (+{len(callees) - 5} more)")

    # ── Grep-based callers ──
    callers = _find_callers(workspace, function_name)
    
    # Rank callers by the PageRank score of the file they live in
    def _caller_rank(caller: dict) -> float:
        f = caller["file"]
        for p, r in repo_map.ranked_files:
            if p == f: return r
        return 0.0
        
    callers_ranked = sorted(callers, key=_caller_rank, reverse=True)[:5]

    result: dict[str, Any] = {
        "name": function_name,
        "file": rel_path,
        "line": target_fn["line"],
        "end_line": target_fn.get("end_line"),
        "params": target_fn.get("params", []),
        "return_type": target_fn.get("return_type"),
        "docstring": target_fn.get("docstring"),
        "callees": callees_ranked,
        "callers": callers_ranked,
    }
    if "class" in target_fn:
        result["class"] = target_fn["class"]

    return result


# ══════════════════════════════════════════════════════════════
# Tier 2 Handlers
# ══════════════════════════════════════════════════════════════


def _handle_mark_as_discussed(args: dict, workspace: Path) -> dict:
    """Mark a code area as discussed in the mental model."""
    args.pop("reasoning", None)
    path = args.get("path", "")
    topic = args.get("topic", "")

    _mental_model[path] = {
        "topic": topic,
        "discussed_at": datetime.now().isoformat(),
    }
    _save_mental_model(workspace)

    return {"status": "ok", "path": path, "total_discussed": len(_mental_model)}


def _handle_write_to_rules(args: dict, workspace: Path) -> dict:
    """Append a project insight to the correct section of .codiey/rules."""
    args.pop("reasoning", None)
    section = args.get("section", "").strip().lower()
    insight = args.get("insight", "").strip()

    if not insight:
        return {"status": "skipped", "reason": "empty insight"}

    SECTION_HEADERS: dict[str, str] = {
        "architecture":    "## Architecture",
        "conventions":     "## Conventions",
        "gotchas":         "## Gotchas",
        "session_history": "## Session History",
    }

    if section not in SECTION_HEADERS:
        return {"status": "error", "reason": f"Unknown section '{section}'. Use: {list(SECTION_HEADERS)}"}

    header = SECTION_HEADERS[section]

    codiey_dir = workspace / ".codiey"
    codiey_dir.mkdir(exist_ok=True)
    rules_path = codiey_dir / "rules"

    with _rules_lock:
        # Read existing content (may be empty or missing)
        try:
            existing = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
        except OSError:
            existing = ""

        # If the file is empty or missing, scaffold all four section headers
        # so the system prompt always sees a complete structure.
        if not existing.strip():
            existing = "\n\n".join(SECTION_HEADERS.values()) + "\n"

        lines = existing.splitlines()

        # Find the target section header
        section_start = None
        for i, line in enumerate(lines):
            if line.strip() == header:
                section_start = i
                break

        if section_start is not None:
            # Find the end of this section (next ## heading or EOF)
            insert_pos = len(lines)
            for i in range(section_start + 1, len(lines)):
                if lines[i].startswith("## "):
                    insert_pos = i
                    break
            # Skip trailing blank lines inside this section before appending
            while insert_pos > section_start + 1 and lines[insert_pos - 1].strip() == "":
                insert_pos -= 1
            lines.insert(insert_pos, insight)
        else:
            # Shouldn't happen after scaffolding, but defensive: append at the end
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(header)
            lines.append(insight)
            lines.append("")

        try:
            rules_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return {"status": "ok", "section": section}
        except OSError as e:
            return {"status": "error", "reason": str(e)}


# ══════════════════════════════════════════════════════════════
# AST Callees Extraction
# ══════════════════════════════════════════════════════════════


def _extract_callees_from_body(
    file_path: Path,
    function_name: str,
    start_line: int,
    end_line: int,
) -> list[str]:
    """Use tree-sitter to extract unique call names from the function body only."""
    from codiey.codebase.parser import EXTENSION_TO_LANGUAGE, get_parser

    ext = file_path.suffix.lower()
    if ext not in EXTENSION_TO_LANGUAGE:
        return []

    try:
        source = file_path.read_bytes()
    except OSError:
        return []

    language = EXTENSION_TO_LANGUAGE[ext]
    parser = get_parser(language)
    tree = parser.parse(source)

    fn_node = _find_function_node(tree.root_node, function_name, start_line)
    if fn_node is None:
        return []

    body_node = _get_function_body(fn_node, ext)
    if body_node is None:
        return []

    callees: set[str] = set()
    is_python = ext == ".py"
    _walk_calls(body_node, callees, is_python)

    # Remove self-recursion and very generic names
    callees.discard(function_name)
    callees.discard("self")
    callees.discard("cls")

    return sorted(callees)


def _find_function_node(root, function_name: str, start_line: int):
    """Walk the AST and find the function node matching name + approximate start line."""
    FUNC_TYPES = {
        "function_definition",          # Python
        "function_declaration",         # JS/TS
        "function_expression",          # JS
        "arrow_function",               # JS
        "method_definition",            # JS class methods
        "generator_function_declaration",
        "generator_function",
        "variable_declarator",          # JS/TS assigned arrow functions
    }

    def walk(node):
        if node.type in FUNC_TYPES:
            nline = node.start_point[0] + 1
            # Allow ±3 lines to account for decorators shifting the line count
            if abs(nline - start_line) <= 3:
                name = _get_node_name(node)
                if name == function_name:
                    return node

        for child in node.children:
            result = walk(child)
            if result is not None:
                return result
        return None

    return walk(root)


def _get_node_name(node) -> str:
    """Extract the name identifier from a function/method AST node."""
    for child in node.children:
        if child.type in ("identifier", "property_identifier"):
            return child.text.decode("utf-8")
    return ""


def _get_function_body(fn_node, ext: str):
    """Return the body node of a function definition."""
    body_type = "block" if ext == ".py" else "statement_block"
    for child in fn_node.children:
        if child.type == body_type:
            return child

    # Arrow functions with expression bodies: return any substantial child
    if ext != ".py":
        for child in fn_node.children:
            if child.type not in (
                "identifier", "formal_parameters", "=>",
                "async", "function", "(",  ")", ":",
                "=", "type_annotation",
            ):
                return child

    return None


def _walk_calls(node, callees: set[str], is_python: bool) -> None:
    """Recursively collect call names from call/call_expression nodes."""
    call_type = "call" if is_python else "call_expression"

    if node.type == call_type:
        name = _get_call_name(node, is_python)
        if name:
            callees.add(name)
        # Recurse into arguments to catch nested calls
        for child in node.children:
            _walk_calls(child, callees, is_python)
        return

    for child in node.children:
        _walk_calls(child, callees, is_python)


def _get_call_name(call_node, is_python: bool) -> str:
    """Extract the callee name from a call expression node."""
    for child in call_node.children:
        # Direct function call: foo(...)
        if child.type == "identifier":
            return child.text.decode("utf-8")
        # Method call: obj.method(...) — Python attribute / JS member_expression
        if child.type in ("attribute", "member_expression"):
            # Rightmost identifier is the method name
            for inner in reversed(child.children):
                if inner.type in ("identifier", "field_identifier", "property_identifier"):
                    return inner.text.decode("utf-8")
    return ""


# ══════════════════════════════════════════════════════════════
# Callers (grep-based)
# ══════════════════════════════════════════════════════════════


def _find_callers(workspace: Path, function_name: str) -> list[dict]:
    """Search all source files for direct calls and method calls to function_name."""
    from codiey.codebase.workspace import walk_source_files

    # Match: function_name( or .function_name( with optional whitespace
    direct = re.compile(r'(?<![.\w])' + re.escape(function_name) + r'\s*\(')
    method = re.compile(r'\.' + re.escape(function_name) + r'\s*\(')

    callers = []
    max_callers = 20

    for abs_path in walk_source_files(workspace):
        try:
            rel = str(abs_path.relative_to(workspace)).replace("\\", "/")
        except ValueError:
            continue

        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for i, line in enumerate(content.split("\n"), 1):
            if direct.search(line) or method.search(line):
                callers.append({
                    "file": rel,
                    "line": i,
                    "snippet": line.strip()[:100],
                })
                if len(callers) >= max_callers:
                    return callers

    return callers


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _resolve_file_path(workspace: Path, file_path: str) -> Path | None:
    """Resolve a relative file path, with fuzzy suffix matching."""
    if not file_path:
        return None

    normalized = file_path.replace("\\", "/")
    candidate = workspace / normalized
    if candidate.exists() and candidate.is_file():
        return candidate

    from codiey.codebase.workspace import walk_all_files

    for abs_path in walk_all_files(workspace):
        try:
            rel = str(abs_path.relative_to(workspace)).replace("\\", "/")
        except ValueError:
            continue
        if rel.endswith(normalized) or normalized.endswith(rel):
            return abs_path

    return None


def _save_mental_model(workspace: Path):
    """Persist mental model to .codiey/mental-model.json."""
    codiey_dir = workspace / ".codiey"
    codiey_dir.mkdir(exist_ok=True)
    model_path = codiey_dir / "mental-model.json"
    with open(model_path, "w", encoding="utf-8") as f:
        json.dump(_mental_model, f, indent=2)
