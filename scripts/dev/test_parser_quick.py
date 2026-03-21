"""Quick smoke test for the parser."""
from pathlib import Path
from codiey.codebase.parser import parse_file
from codiey.codebase.map_builder import build_codebase_map
from codiey.codebase.summary_builder import build_summary

# Test single file parsing
print("=== Parsing codiey/app.py ===")
result = parse_file(Path("codiey/app.py"))
if result:
    print(f"  Functions: {[f['name'] for f in result['functions']]}")
    print(f"  Imports: {len(result['imports'])}")
    print(f"  Lines: {result['line_count']}")
else:
    print("  FAILED to parse")

# Test JS file
print("\n=== Parsing codiey/static/app.js ===")
result_js = parse_file(Path("codiey/static/app.js"))
if result_js:
    print(f"  Functions: {[f['name'] for f in result_js['functions']]}")
    print(f"  Classes: {[c['name'] for c in result_js['classes']]}")
    print(f"  Lines: {result_js['line_count']}")
else:
    print("  FAILED to parse")

# Test full workspace scan
print("\n=== Building codebase map ===")
cmap = build_codebase_map(Path("."), use_cache=False)
print(f"  Total files: {len(cmap.files)}")
print(f"  File counts: {cmap.file_counts}")
print(f"  Total functions: {len(cmap.all_functions)}")
print(f"  Total classes: {len(cmap.all_classes)}")
print(f"  Patterns: {cmap.patterns}")
print(f"  Total lines: {cmap.total_lines}")

# Test summary
print("\n=== Project Summary ===")
summary = build_summary(cmap)
print(summary)
print(f"\n  Summary length: {len(summary)} chars (~{len(summary)//4} tokens)")
