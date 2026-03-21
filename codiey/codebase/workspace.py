"""Shared workspace utilities — directory walking, gitignore, skip logic.

Extracted from map_builder.py so that multiple modules (summary_builder,
handlers, chunker) can reuse them without importing the heavy CodebaseMap.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from .parser import SUPPORTED_EXTENSIONS


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


def should_skip(path: Path) -> bool:
    """Check if this path should be skipped."""
    for part in path.parts:
        if part in SKIP_DIRS:
            return True
        if part.endswith(".egg-info"):
            return True
    return False


def load_gitignore_patterns(workspace: Path) -> list[str]:
    """Load .gitignore patterns (simplified — just exact dir/file names)."""
    gitignore = workspace / ".gitignore"
    if not gitignore.exists():
        return []

    patterns = []
    try:
        for line in gitignore.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line.rstrip("/"))
    except OSError:
        pass
    return patterns


def is_gitignored(rel_path: Path, patterns: list[str]) -> bool:
    """Simplified gitignore matching (covers 90% of cases)."""
    parts = rel_path.parts
    name = rel_path.name

    for pattern in patterns:
        if pattern.startswith("*"):
            suffix = pattern[1:]
            if name.endswith(suffix):
                return True
        elif pattern in parts or name == pattern:
            return True
    return False


def walk_source_files(workspace: Path) -> Iterator[Path]:
    """Yield all parseable source files in the workspace.

    Skips hidden dirs, gitignored paths, and files over MAX_FILE_SIZE.
    Yields absolute paths.
    """
    gitignore_patterns = load_gitignore_patterns(workspace)

    for root, dirs, files in os.walk(workspace):
        root_path = Path(root)
        rel_root = root_path.relative_to(workspace)

        if should_skip(rel_root):
            dirs.clear()
            continue

        # Filter dirs in-place
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS
            and not d.endswith(".egg-info")
            and not is_gitignored(rel_root / d, gitignore_patterns)
        ]

        for filename in files:
            file_path = root_path / filename
            rel_path = file_path.relative_to(workspace)

            if is_gitignored(rel_path, gitignore_patterns):
                continue

            if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            try:
                if file_path.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            yield file_path


def walk_all_files(workspace: Path) -> Iterator[Path]:
    """Yield ALL visible files in the workspace (not just source files).

    Used for grep search — searches through any text file, not just parseable ones.
    """
    gitignore_patterns = load_gitignore_patterns(workspace)

    # Text-like extensions we'll search through
    text_extensions = SUPPORTED_EXTENSIONS | {
        ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
        ".html", ".css", ".scss", ".less", ".svg",
        ".sh", ".bash", ".zsh", ".bat", ".ps1",
        ".env", ".cfg", ".ini", ".conf",
        ".xml", ".csv",
        ".dockerfile", ".dockerignore",
        ".gitignore", ".editorconfig",
    }

    for root, dirs, files in os.walk(workspace):
        root_path = Path(root)
        rel_root = root_path.relative_to(workspace)

        if should_skip(rel_root):
            dirs.clear()
            continue

        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS
            and not d.endswith(".egg-info")
            and not is_gitignored(rel_root / d, gitignore_patterns)
        ]

        for filename in files:
            file_path = root_path / filename
            rel_path = file_path.relative_to(workspace)

            if is_gitignored(rel_path, gitignore_patterns):
                continue

            # Include files with known text extensions or no extension
            ext = file_path.suffix.lower()
            if ext and ext not in text_extensions:
                continue

            try:
                if file_path.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            yield file_path


def build_directory_tree(workspace: Path, max_depth: int = 2) -> str:
    """Build a directory tree string by walking the actual filesystem.

    Unlike the old tree builder, this doesn't depend on CodebaseMap — it
    walks the filesystem directly and includes ALL files (not just parsed ones).
    """
    gitignore_patterns = load_gitignore_patterns(workspace)
    entries: dict[str, list[str]] = {}  # dir_path -> sorted children

    for root, dirs, files in os.walk(workspace):
        root_path = Path(root)
        rel_root = root_path.relative_to(workspace)
        depth = len(rel_root.parts)

        if depth > max_depth:
            dirs.clear()
            continue

        if should_skip(rel_root):
            dirs.clear()
            continue

        # Filter dirs
        dirs[:] = sorted([
            d for d in dirs
            if d not in SKIP_DIRS
            and not d.startswith(".")
            and not d.endswith(".egg-info")
            and not is_gitignored(rel_root / d, gitignore_patterns)
        ])

        key = str(rel_root).replace("\\", "/") if rel_root.parts else "."

        children = []
        # Add subdirectories
        for d in dirs:
            if depth < max_depth:
                children.append(d + "/")

        # Add files (at this level only)
        if depth <= max_depth:
            for f in sorted(files):
                rel_file = rel_root / f
                if not is_gitignored(rel_file, gitignore_patterns):
                    children.append(f)

        if children:
            entries[key] = children

    # Render tree
    lines: list[str] = []
    _render_tree(entries, ".", "", lines, 0, max_depth)
    return "\n".join(lines)


def _render_tree(
    entries: dict[str, list[str]],
    current: str,
    prefix: str,
    lines: list[str],
    depth: int,
    max_depth: int,
):
    if current not in entries:
        return

    children = entries[current]
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        connector = "└── " if is_last else "├── "

        if child.endswith("/"):
            dir_name = child[:-1]
            lines.append(f"{prefix}{connector}{dir_name}/")
            if depth < max_depth:
                new_prefix = prefix + ("    " if is_last else "│   ")
                next_path = f"{current}/{dir_name}" if current != "." else dir_name
                _render_tree(entries, next_path, new_prefix, lines, depth + 1, max_depth)
        else:
            lines.append(f"{prefix}{connector}{child}")
