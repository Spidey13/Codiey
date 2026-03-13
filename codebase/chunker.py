"""AST-boundary chunking — read files without splitting functions or classes.

Uses tree-sitter to find logical node boundaries (functions, classes,
top-level statements) and groups adjacent ones together until a token
budget is hit. A function that's 400 lines stays together as one chunk.

If the file has more content beyond the budget, a continuation hint
tells Gemini how to request the next range.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .parser import EXTENSION_TO_LANGUAGE, get_parser


# 1 token ≈ 4 characters (conservative estimate)
CHARS_PER_TOKEN = 4


def chunk_file(
    file_path: Path,
    token_budget: int = 4000,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Read a file using AST-boundary chunking.

    If start_line/end_line are provided, reads that range (still snapping
    to AST boundaries). Otherwise reads from the top within the token budget.

    Returns:
        {
            "file": "relative/path.py",
            "language": "python",
            "line_count": 232,
            "content": "...the chunk...",
            "start_line": 1,
            "end_line": 85,
            "symbols": ["func_a", "ClassB"],
            "has_more": True,
            "continuation": "Lines 86-232 remain. Use read_file with start_line=86 to continue."
        }
    """
    ext = file_path.suffix.lower()

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return {"error": f"Cannot read file: {file_path}"}

    source_lines = source.split("\n")
    total_lines = len(source_lines)

    # ── Non-parseable files: fall back to character-budget slicing ──
    if ext not in EXTENSION_TO_LANGUAGE:
        return _chunk_raw(source_lines, total_lines, token_budget, start_line, end_line)

    # ── Parse with tree-sitter to find AST boundaries ──
    language = EXTENSION_TO_LANGUAGE[ext]
    parser = get_parser(language)
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node

    # Collect top-level AST nodes with their line ranges
    nodes = _collect_top_level_nodes(root, ext)

    if not nodes:
        # No parseable nodes — treat as raw text
        return _chunk_raw(source_lines, total_lines, token_budget, start_line, end_line)

    # ── Apply line range filter if specified ──
    if start_line is not None:
        nodes = [n for n in nodes if n["end_line"] >= start_line]
    if end_line is not None:
        nodes = [n for n in nodes if n["start_line"] <= end_line]

    if not nodes:
        # Requested range has no AST nodes — fall back to raw
        return _chunk_raw(source_lines, total_lines, token_budget, start_line, end_line)

    # ── Group nodes into a chunk within the token budget ──
    char_budget = token_budget * CHARS_PER_TOKEN
    chunk_nodes: list[dict] = []
    chunk_chars = 0

    for node in nodes:
        node_text = "\n".join(source_lines[node["start_line"] - 1 : node["end_line"]])
        node_chars = len(node_text)

        # Always include at least one node (even if it exceeds budget)
        if chunk_nodes and chunk_chars + node_chars > char_budget:
            break

        chunk_nodes.append(node)
        chunk_chars += node_chars

    if not chunk_nodes:
        return _chunk_raw(source_lines, total_lines, token_budget, start_line, end_line)

    # ── Build the chunk content ──
    chunk_start = chunk_nodes[0]["start_line"]
    chunk_end = chunk_nodes[-1]["end_line"]

    # Include any leading content (module docstring, imports) if starting from top
    actual_start = chunk_start
    if start_line is None and chunk_start > 1:
        # Include lines before the first node (imports, comments, etc.)
        actual_start = 1

    content = "\n".join(source_lines[actual_start - 1 : chunk_end])
    symbols = [n["name"] for n in chunk_nodes if n.get("name")]

    result: dict[str, Any] = {
        "line_count": total_lines,
        "content": content,
        "start_line": actual_start,
        "end_line": chunk_end,
        "symbols": symbols,
    }

    # ── Continuation hint ──
    if chunk_end < total_lines:
        remaining = total_lines - chunk_end
        result["has_more"] = True
        result["continuation"] = (
            f"Lines {chunk_end + 1}-{total_lines} remain ({remaining} lines). "
            f"Use read_file with start_line={chunk_end + 1} to continue."
        )
    else:
        result["has_more"] = False

    return result


def _collect_top_level_nodes(root_node, ext: str) -> list[dict]:
    """Collect top-level AST nodes with their names and line ranges."""
    nodes = []

    for child in root_node.children:
        node_info = _node_to_info(child, ext)
        if node_info:
            nodes.append(node_info)

    return nodes


def _node_to_info(node, ext: str) -> dict | None:
    """Extract info from a single AST node."""
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    name = ""

    # Python nodes
    if node.type in ("function_definition", "class_definition"):
        name = _get_child_text(node, "identifier")
    elif node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                name = _get_child_text(child, "identifier")
                break
    # JS/TS nodes
    elif node.type in ("function_declaration", "generator_function_declaration", "class_declaration"):
        name = _get_child_text(node, "identifier")
    elif node.type == "export_statement":
        # Dig into export to find the actual declaration
        for child in node.children:
            inner = _node_to_info(child, ext)
            if inner:
                inner["start_line"] = start_line  # Include the export keyword
                return inner
        # Generic export without a named inner declaration
        name = "(export)"
    elif node.type == "lexical_declaration":
        # const foo = () => {}
        for child in node.children:
            if child.type == "variable_declarator":
                name = _get_child_text(child, "identifier")
                break
    elif node.type in ("import_statement", "import_declaration"):
        name = "(import)"
    elif node.type in ("expression_statement", "comment"):
        # Top-level expressions — don't give them individual entries
        # but include them in adjacent blocks
        return {
            "type": node.type,
            "name": "",
            "start_line": start_line,
            "end_line": end_line,
        }
    else:
        # Any other top-level node
        return {
            "type": node.type,
            "name": "",
            "start_line": start_line,
            "end_line": end_line,
        }

    return {
        "type": node.type,
        "name": name,
        "start_line": start_line,
        "end_line": end_line,
    }


def _get_child_text(node, child_type: str) -> str:
    """Get the text of the first child of a given type."""
    for child in node.children:
        if child.type == child_type:
            return child.text.decode("utf-8")
    return ""


def _chunk_raw(
    source_lines: list[str],
    total_lines: int,
    token_budget: int,
    start_line: int | None,
    end_line: int | None,
) -> dict[str, Any]:
    """Fallback chunking for non-parseable files — simple character budget."""
    char_budget = token_budget * CHARS_PER_TOKEN

    s = (start_line or 1) - 1  # Convert to 0-indexed
    e = end_line or total_lines

    content_lines = source_lines[s:e]
    content = "\n".join(content_lines)

    # Trim to budget
    if len(content) > char_budget:
        # Find the last newline within budget
        trimmed = content[:char_budget]
        last_nl = trimmed.rfind("\n")
        if last_nl > 0:
            trimmed = trimmed[:last_nl]
        content = trimmed
        actual_end = s + content.count("\n") + 1
    else:
        actual_end = e

    result: dict[str, Any] = {
        "line_count": total_lines,
        "content": content,
        "start_line": s + 1,
        "end_line": actual_end,
        "symbols": [],
    }

    if actual_end < total_lines:
        remaining = total_lines - actual_end
        result["has_more"] = True
        result["continuation"] = (
            f"Lines {actual_end + 1}-{total_lines} remain ({remaining} lines). "
            f"Use read_file with start_line={actual_end + 1} to continue."
        )
    else:
        result["has_more"] = False

    return result
