"""Test the new retrieval architecture — on-demand tools, AST chunking, grep search."""
from pathlib import Path
import json

WORKSPACE = Path(".")

# ══════════════════════════════════════════════════════════════
# TEST 1: Lightweight Summary
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("TEST 1: Lightweight Summary")
print("=" * 60)
from codiey.codebase.summary_builder import build_lightweight_summary

summary = build_lightweight_summary(WORKSPACE)
print(f"  Length: {len(summary)} chars (~{len(summary)//4} tokens)")
print(f"  Preview:\n{summary[:500]}")

# Verify it does NOT contain function/class listings
assert "## Functions" not in summary, "Summary should NOT list functions"
assert "## Classes" not in summary, "Summary should NOT list classes"
assert "Directory Structure" in summary, "Summary should contain directory tree"
print("  ✅ Summary is lightweight (no function/class listings)")

# ══════════════════════════════════════════════════════════════
# TEST 2: AST-Boundary Chunking
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 2: AST-Boundary Chunking")
print("=" * 60)
from codiey.codebase.chunker import chunk_file

# Chunk a Python file
result = chunk_file(Path("codiey/app.py"), token_budget=2000)
print(f"  File: codiey/app.py")
print(f"  Chunk lines: {result.get('start_line')}-{result.get('end_line')}")
print(f"  Symbols: {result.get('symbols', [])}")
print(f"  Has more: {result.get('has_more')}")
if result.get('continuation'):
    print(f"  Continuation: {result['continuation']}")
assert result.get("start_line") is not None, "Should have start_line"
assert result.get("content"), "Should have content"
print("  ✅ AST chunking works for Python")

# Chunk a JS file
result_js = chunk_file(Path("codiey/static/app.js"), token_budget=2000)
print(f"\n  File: codiey/static/app.js")
print(f"  Chunk lines: {result_js.get('start_line')}-{result_js.get('end_line')}")
print(f"  Symbols: {result_js.get('symbols', [])[:5]}...")
print(f"  Has more: {result_js.get('has_more')}")
assert result_js.get("content"), "Should have content"
print("  ✅ AST chunking works for JavaScript")

# Test continuation (read from specific line)
if result.get("has_more"):
    # Parse out the start line from continuation
    cont_start = result["end_line"] + 1
    result2 = chunk_file(Path("codiey/app.py"), token_budget=2000, start_line=cont_start)
    print(f"\n  Continuation chunk: lines {result2.get('start_line')}-{result2.get('end_line')}")
    print(f"  Symbols: {result2.get('symbols', [])}")
    assert result2.get("start_line", 0) >= cont_start, "Continuation should start at or after requested line"
    print("  ✅ Continuation reading works")

# ══════════════════════════════════════════════════════════════
# TEST 3: Tool Execution — read_file
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 3: Tool Execution — read_file")
print("=" * 60)
from codiey.tools.handlers import execute_tool

result = execute_tool("read_file", {"file_path": "codiey/app.py"}, WORKSPACE)
print(f"  File: {result.get('file')}")
print(f"  Lines: {result.get('start_line')}-{result.get('end_line')} of {result.get('line_count')}")
print(f"  Symbols: {result.get('symbols', [])[:5]}")
print(f"  Has more: {result.get('has_more')}")
assert "error" not in result, f"Tool failed: {result.get('error')}"
assert result.get("content"), "Should return file content"
print("  ✅ read_file tool works")

# ══════════════════════════════════════════════════════════════
# TEST 4: Tool Execution — get_file_structure
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 4: Tool Execution — get_file_structure")
print("=" * 60)

result = execute_tool("get_file_structure", {"file_path": "codiey/app.py"}, WORKSPACE)
print(f"  File: {result.get('file')}")
print(f"  Functions: {[f['name'] for f in result.get('functions', [])]}")
print(f"  Line count: {result.get('line_count')}")
assert "error" not in result, f"Tool failed: {result.get('error')}"
assert len(result.get("functions", [])) > 0, "Should find functions"
print("  ✅ get_file_structure tool works")

# ══════════════════════════════════════════════════════════════
# TEST 5: Tool Execution — grep_search
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 5: Tool Execution — grep_search")
print("=" * 60)

# Literal search
result = execute_tool("grep_search", {"query": "GEMINI_MODEL"}, WORKSPACE)
print(f"  Query: 'GEMINI_MODEL'")
print(f"  Total matches: {result.get('total_matches')}")
for r in result.get("results", [])[:5]:
    print(f"    {r['file']}:{r['line']} → {r['content'][:60]}")
assert result.get("total_matches", 0) > 0, "Should find GEMINI_MODEL"
print("  ✅ grep_search (literal) works")

# With file extension filter
result = execute_tool("grep_search", {"query": "FastAPI", "include": "py"}, WORKSPACE)
print(f"\n  Query: 'FastAPI' (include=py)")
print(f"  Total matches: {result.get('total_matches')}")
print(f"  Files searched: {result.get('files_searched')}")
assert result.get("total_matches", 0) > 0, "Should find FastAPI in .py files"
print("  ✅ grep_search (with extension filter) works")

# ══════════════════════════════════════════════════════════════
# TEST 6: Tool Execution — list_symbols
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 6: Tool Execution — list_symbols")
print("=" * 60)

result = execute_tool("list_symbols", {"query": "parse"}, WORKSPACE)
print(f"  Query: 'parse'")
print(f"  Total matches: {result.get('total_matches')}")
for r in result.get("results", [])[:5]:
    print(f"    {r['type']}: {r['name']} in {r['file']}:{r['line']}")
assert result.get("total_matches", 0) > 0, "Should find parse_file"
# Check that parse_file is in the results
names = [r["name"] for r in result.get("results", [])]
assert "parse_file" in names, "Should find parse_file function"
print("  ✅ list_symbols works")

# ══════════════════════════════════════════════════════════════
# TEST 7: Tool Execution — mark_as_discussed
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 7: Tool Execution — mark_as_discussed")
print("=" * 60)

result = execute_tool("mark_as_discussed", {"path": "codiey/app.py", "topic": "FastAPI routing"}, WORKSPACE)
print(f"  Status: {result['status']}")
print(f"  Total discussed: {result['total_discussed']}")
assert result["status"] == "ok"
print("  ✅ mark_as_discussed works")

# ══════════════════════════════════════════════════════════════
# TEST 8: Tool Declarations
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 8: Tool Declarations")
print("=" * 60)

from codiey.tools.declarations import TOOL_DECLARATIONS
tool_names = [fd["name"] for fd in TOOL_DECLARATIONS[0]["functionDeclarations"]]
print(f"  Tools: {tool_names}")

TIER1_TOOLS = {"grep_search", "file_search", "list_directory", "read_file", "get_function_info"}
TIER2_TOOLS = {"mark_as_discussed", "write_to_rules"}
expected_tools = TIER1_TOOLS | TIER2_TOOLS
assert set(tool_names) == expected_tools, f"Expected {expected_tools}, got {set(tool_names)}"

# All tools must have a required 'reasoning' parameter
for fd in TOOL_DECLARATIONS[0]["functionDeclarations"]:
    assert "reasoning" in fd["parameters"]["required"], f"{fd['name']} missing required 'reasoning'"
    assert len(fd["description"].strip().rstrip(".")) > 0, f"{fd['name']} has empty description"

print(f"  Tier 1: {sorted(TIER1_TOOLS)}")
print(f"  Tier 2: {sorted(TIER2_TOOLS)}")
print("  ✅ Tool declarations correct")

print("\n" + "=" * 60)
print("ALL TESTS PASSED ✅")
print("=" * 60)
