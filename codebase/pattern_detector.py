"""Heuristic framework and pattern detection.

Scans the CodebaseMap for known framework signatures:
  - FastAPI, Flask, Django (Python)
  - Express, Next.js, React (JS/TS)
  - Common patterns: tests, CI/CD, Docker, etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .map_builder import CodebaseMap


def detect_patterns(cmap: "CodebaseMap") -> list[str]:
    """Detect frameworks and patterns in the codebase.

    Returns a list of pattern strings like:
        ["FastAPI", "Python", "pytest", "Docker", ...]
    """
    patterns = []

    files = set(cmap.files.keys())
    all_imports_flat = set()
    for file_data in cmap.files.values():
        for imp in file_data.get("imports", []):
            module = imp.get("module", "") or ""
            all_imports_flat.add(module.split(".")[0])
            for name in imp.get("names", []):
                clean = name.split(" as ")[0].strip()
                all_imports_flat.add(clean)

    # ── Python Frameworks ──
    if "fastapi" in all_imports_flat or "FastAPI" in all_imports_flat:
        patterns.append("FastAPI")
    if "flask" in all_imports_flat or "Flask" in all_imports_flat:
        patterns.append("Flask")
    if "django" in all_imports_flat:
        patterns.append("Django")
    if "celery" in all_imports_flat:
        patterns.append("Celery")
    if "sqlalchemy" in all_imports_flat:
        patterns.append("SQLAlchemy")
    if "pydantic" in all_imports_flat:
        patterns.append("Pydantic")
    if "pytest" in all_imports_flat or any("test" in f for f in files):
        patterns.append("pytest")
    if "click" in all_imports_flat:
        patterns.append("Click CLI")
    if "asyncio" in all_imports_flat:
        patterns.append("asyncio")

    # ── JS/TS Frameworks ──
    if "react" in all_imports_flat or "React" in all_imports_flat:
        patterns.append("React")
    if "next" in all_imports_flat or any("next.config" in f for f in files):
        patterns.append("Next.js")
    if "express" in all_imports_flat:
        patterns.append("Express")
    if "vue" in all_imports_flat:
        patterns.append("Vue.js")
    if "angular" in all_imports_flat:
        patterns.append("Angular")
    if "svelte" in all_imports_flat:
        patterns.append("Svelte")

    # ── Infrastructure Patterns ──
    if any("Dockerfile" in f or "docker-compose" in f for f in files):
        patterns.append("Docker")
    if any(".github" in f for f in files):
        patterns.append("GitHub Actions")
    if any("terraform" in f.lower() for f in files):
        patterns.append("Terraform")
    if any("pyproject.toml" in f for f in files):
        patterns.append("pyproject.toml")
    if any("package.json" in f for f in files):
        patterns.append("npm/node")
    if any(".env" in f for f in files):
        patterns.append("dotenv")

    # ── Languages ──
    if cmap.file_counts.get("python", 0) > 0:
        patterns.append("Python")
    if cmap.file_counts.get("javascript", 0) > 0:
        patterns.append("JavaScript")
    if cmap.file_counts.get("typescript", 0) > 0:
        patterns.append("TypeScript")

    return sorted(set(patterns))
