"""Hexagon-purity guard: the domain core imports no adapter/web/I-O libraries.

Walks every module under ``src/plex_manager/domain`` and parses its imports with
``ast``. The domain may depend only on stdlib, pydantic, and ``plex_manager``
(itself / ports). Any import of an adapter, the web layer, or a concrete I/O
library (httpx, sqlalchemy, guessit) is a north-star violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

_DOMAIN_DIR = Path(__file__).resolve().parents[2] / "src" / "plex_manager" / "domain"

_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "httpx",
    "sqlalchemy",
    "guessit",
    "fastapi",
    "plex_manager.adapters",
    "plex_manager.web",
    "plex_manager.models",
    "plex_manager.repositories",
)


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return names


def test_domain_modules_have_no_forbidden_imports() -> None:
    domain_files = sorted(_DOMAIN_DIR.glob("*.py"))
    assert domain_files, "expected domain modules to scan"

    violations: list[str] = []
    for path in domain_files:
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in _FORBIDDEN_PREFIXES
            ):
                violations.append(f"{path.name}: {module}")

    assert not violations, f"forbidden domain imports: {violations}"
