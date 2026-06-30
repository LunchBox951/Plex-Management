"""Export the app's OpenAPI document to ``docs/api/openapi.json`` (FE handoff).

Run as ``python -m plex_manager.web.openapi_export`` (or ``make openapi``). The
schema is derived from :func:`create_app`, so it always reflects the wired routers.
Regenerate after any router / schema change and commit the result — there is no
drift-failing test gating it (the file is a handoff artifact, not an assertion).
"""

from __future__ import annotations

import json
from pathlib import Path

from plex_manager.web.app import create_app

__all__ = ["DEFAULT_OUTPUT", "export_openapi", "main"]

# Repo-relative default: <repo>/docs/api/openapi.json (this file is
# <repo>/src/plex_manager/web/openapi_export.py -> four parents up to <repo>).
DEFAULT_OUTPUT: Path = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"


def export_openapi(output: Path | None = None) -> Path:
    """Write the pretty-printed OpenAPI document; return the path written."""
    destination = output or DEFAULT_OUTPUT
    destination.parent.mkdir(parents=True, exist_ok=True)
    spec = create_app().openapi()
    destination.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def main() -> None:
    """CLI entry point: export to the default location and report the path."""
    written = export_openapi()
    print(f"wrote {written}")


if __name__ == "__main__":
    main()
