"""Generate a lightweight project summary for the system prompt.

Only includes:
  - 2-level directory tree (from filesystem, not parsed files)
  - Language breakdown (by extension scanning)
  - Contents of .codiey/rules if present

This replaces the old summary_builder that listed every function/class.
The system prompt stays small; Gemini uses tools for structural lookups.
"""

from __future__ import annotations

from pathlib import Path

from .workspace import build_directory_tree, walk_source_files, SKIP_DIRS


def build_lightweight_summary(workspace: Path) -> str:
    """Build a compact project summary for injection into the system prompt.

    Rules come first — they contain verified project truths and must take
    precedence over everything else in context. Directory structure follows.
    Everything else is retrieved on demand via tool calls.
    """
    sections: list[str] = []

    # ── Rules file — FIRST, before all other content ──
    # Rules are verified facts about this project. They override any assumptions
    # Gemini might form from the directory structure or general knowledge.
    rules_path = workspace / ".codiey" / "rules"
    if rules_path.exists():
        try:
            rules_content = rules_path.read_text(encoding="utf-8").strip()
            if rules_content:
                sections.append("## Project Rules")
                sections.append(rules_content)
                sections.append("")
        except OSError:
            pass

    # ── Project header + language breakdown ──
    workspace_name = workspace.name
    sections.append(f"# Project: {workspace_name}")

    ext_counts: dict[str, int] = {}
    file_count = 0
    for file_path in walk_source_files(workspace):
        file_count += 1
        ext = file_path.suffix.lower()
        lang = _ext_to_lang(ext)
        ext_counts[lang] = ext_counts.get(lang, 0) + 1

    sections.append(f"Source files: {file_count}")
    if ext_counts:
        lang_parts = [f"{lang}: {count}" for lang, count in sorted(ext_counts.items())]
        sections.append(f"Languages: {', '.join(lang_parts)}")

    sections.append("")

    # ── Directory tree (top 2 levels) ──
    sections.append("## Directory Structure")
    tree = build_directory_tree(workspace, max_depth=2)
    sections.append(tree)
    sections.append("")

    # ── Top 10 Ranked Files ──
    from codiey.app import get_repo_map
    repo_map = get_repo_map()
    if repo_map.ranked_files:
        sections.append("## Key Files (by importance)")
        for i, (f_path, rank) in enumerate(repo_map.ranked_files[:10], 1):
            sections.append(f"{i}. {f_path}")
        sections.append("")

    return "\n".join(sections)


def _ext_to_lang(ext: str) -> str:
    """Map file extension to language name."""
    mapping = {
        ".py": "Python",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".mjs": "JavaScript",
        ".cjs": "JavaScript",
    }
    return mapping.get(ext, ext)
