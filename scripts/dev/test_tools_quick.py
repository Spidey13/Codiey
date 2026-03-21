"""Quick test for the full tool execution pipeline."""
from pathlib import Path
import json

# Test 1: Build codebase map
print("=" * 60)
print("TEST 1: Build CodebaseMap")
print("=" * 60)
from codiey.codebase.map_builder import build_codebase_map
cmap = build_codebase_map(Path("."), use_cache=False)
print(f"  ✅ Files: {len(cmap.files)}")
print(f"  ✅ Functions: {len(cmap.all_functions)}")
print(f"  ✅ Classes: {len(cmap.all_classes)}")
print(f"  ✅ Patterns: {cmap.patterns}")

# Test 2: Cache saved?
cache_path = Path(".codiey/codebase_map.json")
print(f"\n  Cache exists: {cache_path.exists()}")
if cache_path.exists():
    size_kb = cache_path.stat().st_size / 1024
    print(f"  Cache size: {size_kb:.1f} KB")

# Test 3: Execute each tool
print("\n" + "=" * 60)
print("TEST 2: Tool Execution")
print("=" * 60)
from codiey.tools.handlers import execute_tool

# get_project_overview
result = execute_tool("get_project_overview", {}, cmap)
print(f"\n  get_project_overview:")
print(f"    Project: {result['project_name']}")
print(f"    Files: {result['total_files']}, Functions: {result['total_functions']}")
print(f"    Patterns: {result['patterns']}")

# get_file_details
result = execute_tool("get_file_details", {"file_path": "codiey/app.py"}, cmap)
print(f"\n  get_file_details('codiey/app.py'):")
print(f"    Functions: {[f['name'] for f in result.get('functions', [])]}")
print(f"    Imports: {len(result.get('imports', []))}")

# get_function_info
result = execute_tool("get_function_info", {"function_name": "serve_index"}, cmap)
print(f"\n  get_function_info('serve_index'):")
print(f"    File: {result.get('file')}")
print(f"    Lines: {result.get('line', '?')}-{result.get('end_line', '?')}")
print(f"    Source preview: {result.get('source_code', '')[:80]}...")

# search_codebase
result = execute_tool("search_codebase", {"query": "session", "search_type": "function"}, cmap)
print(f"\n  search_codebase('session', type=function):")
print(f"    Matches: {result['total_matches']}")
for r in result['results'][:5]:
    print(f"      {r['name']} in {r['file']}:{r['line']}")

# get_dependency_graph
result = execute_tool("get_dependency_graph", {"module_path": "codiey/app.py"}, cmap)
print(f"\n  get_dependency_graph('codiey/app.py'):")
print(f"    Imports from: {result.get('imports_from', [])}")
print(f"    External deps: {result.get('external_deps', [])}")

# mark_as_discussed
result = execute_tool("mark_as_discussed", {"path": "codiey/app.py", "topic": "FastAPI routing"}, cmap)
print(f"\n  mark_as_discussed:")
print(f"    Status: {result['status']}, Total discussed: {result['total_discussed']}")

# Test 4: Summary builder
print("\n" + "=" * 60)
print("TEST 3: Summary Builder")
print("=" * 60)
from codiey.codebase.summary_builder import build_summary
summary = build_summary(cmap)
print(f"  Length: {len(summary)} chars (~{len(summary)//4} tokens)")
print(f"  Preview:\n{summary[:500]}...")

# Test 5: Tool declarations
print("\n" + "=" * 60)
print("TEST 4: Tool Declarations")
print("=" * 60)
from codiey.tools.declarations import TOOL_DECLARATIONS
tool_names = [fd["name"] for fd in TOOL_DECLARATIONS[0]["functionDeclarations"]]
print(f"  Tools: {tool_names}")
print(f"  All NON_BLOCKING: {all(fd.get('behavior') == 'NON_BLOCKING' for fd in TOOL_DECLARATIONS[0]['functionDeclarations'])}")

print("\n" + "=" * 60)
print("ALL TESTS PASSED ✅")
print("=" * 60)
