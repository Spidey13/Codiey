"""Module-level import/dependency graph.

For each file in the codebase, determine:
  - What it imports (internal modules and external packages)
  - Which other files import it
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .map_builder import CodebaseMap


def build_dependency_graph(cmap: "CodebaseMap") -> dict[str, dict[str, Any]]:
    """Build a module dependency graph.

    Returns:
        {
            "codiey/app.py": {
                "imports": ["fastapi", "codiey.cli", ...],
                "imported_by": ["codiey/main.py", ...],
                "internal_imports": ["codiey/cli.py"],
                "external_imports": ["fastapi", "uvicorn"],
            }
        }
    """
    # Build a set of all internal module paths for resolution
    internal_modules = set(cmap.files.keys())

    # Map module dotted names to file paths
    module_to_file: dict[str, str] = {}
    for rel_path in internal_modules:
        # codiey/app.py -> codiey.app
        dotted = rel_path.replace("/", ".").replace("\\", ".")
        if dotted.endswith(".py"):
            dotted = dotted[:-3]
        module_to_file[dotted] = rel_path

        # Also map just the filename stem
        stem = Path(rel_path).stem
        if stem != "__init__":
            module_to_file[stem] = rel_path

    graph: dict[str, dict[str, Any]] = {}

    for rel_path, file_data in cmap.files.items():
        internal_imports = []
        external_imports = []

        for imp in file_data.get("imports", []):
            module = imp.get("module") or ""
            names = imp.get("names", [])

            # Try to resolve to internal files
            resolved = _resolve_import(module, names, module_to_file, rel_path)

            if resolved:
                internal_imports.extend(resolved)
            else:
                # It's an external package
                pkg_name = module.split(".")[0] if module else (names[0].split(".")[0] if names else "")
                if pkg_name:
                    external_imports.append(pkg_name)

        graph[rel_path] = {
            "imports": internal_imports + external_imports,
            "imported_by": [],  # Filled in second pass
            "internal_imports": internal_imports,
            "external_imports": list(set(external_imports)),
        }

    # Second pass: fill imported_by
    for rel_path, deps in graph.items():
        for internal in deps["internal_imports"]:
            if internal in graph:
                graph[internal]["imported_by"].append(rel_path)

    return graph


def _resolve_import(
    module: str,
    names: list[str],
    module_to_file: dict[str, str],
    current_file: str,
) -> list[str]:
    """Try to resolve an import to internal file paths."""
    resolved = []

    if module:
        # Handle relative imports (leading dots)
        if module.startswith("."):
            abs_module = _resolve_relative_import(module, current_file)
            if abs_module and abs_module in module_to_file:
                resolved.append(module_to_file[abs_module])
        elif module in module_to_file:
            resolved.append(module_to_file[module])
        else:
            # Try partial match (e.g., "codiey.codebase" matches "codiey/codebase/__init__.py")
            for dotted, file_path in module_to_file.items():
                if dotted.startswith(module + ".") or dotted == module:
                    resolved.append(file_path)
                    break

    # Also check individual imported names
    for name in names:
        clean_name = name.split(" as ")[0].strip() if " as " in name else name
        if clean_name in module_to_file:
            resolved.append(module_to_file[clean_name])

    return list(set(resolved))


def _resolve_relative_import(module: str, current_file: str) -> str | None:
    """Convert a relative import like '.parser' to an absolute module name."""
    # Count leading dots
    dots = 0
    for ch in module:
        if ch == ".":
            dots += 1
        else:
            break

    remainder = module[dots:]

    # Get the package of the current file
    parts = current_file.replace("\\", "/").split("/")
    # Remove the filename
    if parts:
        parts = parts[:-1]

    # Go up (dots - 1) levels
    for _ in range(dots - 1):
        if parts:
            parts.pop()

    if remainder:
        parts.append(remainder)

    return ".".join(parts) if parts else None


def get_module_dependencies(cmap: "CodebaseMap", module_path: str) -> dict[str, Any]:
    """Get dependency info for a specific module.

    Args:
        module_path: Relative path like 'codiey/app.py'

    Returns:
        {
            "module": "codiey/app.py",
            "imports_from": [...],
            "imported_by": [...],
            "external_deps": [...],
        }
    """
    graph = build_dependency_graph(cmap)

    # Normalize path
    normalized = module_path.replace("\\", "/")

    if normalized not in graph:
        # Try fuzzy match
        for key in graph:
            if key.endswith(normalized) or normalized.endswith(key):
                normalized = key
                break
        else:
            return {
                "module": module_path,
                "imports_from": [],
                "imported_by": [],
                "external_deps": [],
                "error": f"Module '{module_path}' not found in codebase",
            }

    entry = graph[normalized]
    return {
        "module": normalized,
        "imports_from": entry["internal_imports"],
        "imported_by": entry["imported_by"],
        "external_deps": entry["external_imports"],
    }
