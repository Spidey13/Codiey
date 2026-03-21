"""Build a CodebaseMap by walking the workspace and parsing all supported files.

The CodebaseMap is a dict-based graph of all files, functions, classes, and
their locations. It can be serialized to JSON and cached in `.codiey/`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .parser import SUPPORTED_EXTENSIONS, parse_file


# Directories to always skip
SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".venv", "venv", "env", ".env",
    "dist", "build", ".next", ".nuxt",
    ".codiey", ".vscode", ".idea",
    "coverage", ".coverage",
    "egg-info",
}

# Max file size to parse (skip large generated files)
MAX_FILE_SIZE = 500_000  # 500 KB


class CodebaseMap:
    """In-memory representation of a parsed workspace."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.files: dict[str, dict[str, Any]] = {}  # relative_path -> parsed data
        self.all_functions: list[dict] = []
        self.all_classes: list[dict] = []
        self.all_imports: list[dict] = []
        self.file_counts: dict[str, int] = {}  # language -> count
        self.total_lines: int = 0
        self.patterns: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "workspace": str(self.workspace),
            "files": self.files,
            "file_counts": self.file_counts,
            "total_lines": self.total_lines,
            "patterns": self.patterns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CodebaseMap":
        """Deserialize from a cached dict."""
        cmap = cls(Path(data["workspace"]))
        cmap.files = data.get("files", {})
        cmap.file_counts = data.get("file_counts", {})
        cmap.total_lines = data.get("total_lines", 0)
        cmap.patterns = data.get("patterns", [])

        # Rebuild the flat lists
        for rel_path, file_data in cmap.files.items():
            for fn in file_data.get("functions", []):
                fn_with_file = {**fn, "file": rel_path}
                cmap.all_functions.append(fn_with_file)
            for cls_data in file_data.get("classes", []):
                cls_with_file = {**cls_data, "file": rel_path}
                cmap.all_classes.append(cls_with_file)
            for imp in file_data.get("imports", []):
                imp_with_file = {**imp, "file": rel_path}
                cmap.all_imports.append(imp_with_file)

        return cmap

    def save_cache(self) -> Path:
        """Save to .codiey/codebase_map.json."""
        cache_dir = self.workspace / ".codiey"
        cache_dir.mkdir(exist_ok=True)
        cache_path = cache_dir / "codebase_map.json"
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        return cache_path

    @classmethod
    def load_cache(cls, workspace: Path) -> "CodebaseMap | None":
        """Load from .codiey/codebase_map.json if it exists."""
        cache_path = workspace / ".codiey" / "codebase_map.json"
        if not cache_path.exists():
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (json.JSONDecodeError, KeyError, OSError):
            return None


def _should_skip(path: Path) -> bool:
    """Check if this path should be skipped."""
    for part in path.parts:
        if part in SKIP_DIRS:
            return True
        if part.endswith(".egg-info"):
            return True
    return False


def _load_gitignore_patterns(workspace: Path) -> list[str]:
    """Load .gitignore patterns (simplified — just exact dir/file names)."""
    gitignore = workspace / ".gitignore"
    if not gitignore.exists():
        return []

    patterns = []
    try:
        for line in gitignore.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                # Strip trailing slashes for directory patterns
                patterns.append(line.rstrip("/"))
    except OSError:
        pass
    return patterns


def _is_gitignored(rel_path: Path, patterns: list[str]) -> bool:
    """Simplified gitignore matching (covers 90% of cases)."""
    parts = rel_path.parts
    name = rel_path.name

    for pattern in patterns:
        # Direct name match (e.g., "node_modules", "*.pyc")
        if pattern.startswith("*"):
            suffix = pattern[1:]
            if name.endswith(suffix):
                return True
        elif pattern in parts or name == pattern:
            return True
    return False


def build_codebase_map(workspace: Path, use_cache: bool = True) -> CodebaseMap:
    """Walk the workspace, parse all supported files, build the map.

    Args:
        workspace: Root directory of the project.
        use_cache: If True, try to load from .codiey/codebase_map.json first.

    Returns:
        A populated CodebaseMap.
    """
    # Try cache first
    if use_cache:
        cached = CodebaseMap.load_cache(workspace)
        if cached is not None:
            return cached

    cmap = CodebaseMap(workspace)
    gitignore_patterns = _load_gitignore_patterns(workspace)

    for root, dirs, files in os.walk(workspace):
        root_path = Path(root)
        rel_root = root_path.relative_to(workspace)

        # Skip known directories
        if _should_skip(rel_root):
            dirs.clear()
            continue

        # Filter dirs in-place to prevent descending
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS
            and not d.endswith(".egg-info")
            and not _is_gitignored(rel_root / d, gitignore_patterns)
        ]

        for filename in files:
            file_path = root_path / filename
            rel_path = file_path.relative_to(workspace)

            # Skip gitignored files
            if _is_gitignored(rel_path, gitignore_patterns):
                continue

            # Skip unsupported extensions
            if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            # Skip large files
            try:
                if file_path.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            # Parse the file
            parsed = parse_file(file_path)
            if parsed is None:
                continue

            rel_str = str(rel_path).replace("\\", "/")
            cmap.files[rel_str] = parsed
            cmap.total_lines += parsed.get("line_count", 0)

            # Track language counts
            lang = parsed.get("language", "unknown")
            cmap.file_counts[lang] = cmap.file_counts.get(lang, 0) + 1

            # Build flat indexes
            for fn in parsed.get("functions", []):
                cmap.all_functions.append({**fn, "file": rel_str})
            for cls_data in parsed.get("classes", []):
                cmap.all_classes.append({**cls_data, "file": rel_str})
            for imp in parsed.get("imports", []):
                cmap.all_imports.append({**imp, "file": rel_str})

    # Detect patterns
    from .pattern_detector import detect_patterns
    cmap.patterns = detect_patterns(cmap)

    # Cache to disk
    cmap.save_cache()

    return cmap
