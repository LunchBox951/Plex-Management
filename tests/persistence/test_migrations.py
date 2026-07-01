"""The Alembic migration chain actually runs — upgrade head + downgrade base.

The rest of the suite builds its schema via ``Base.metadata.create_all`` (models),
so a migration that references a not-yet-created object, or fails to reverse, is
invisible to it and would only surface at DEPLOY time (`alembic upgrade` on a real
DB). This exercises the real chain against a throwaway SQLite file, including the
EXISTING-INSTALL path (stamp at the pre-TV revision, then upgrade), which is the
scenario a create_all-based test can never cover.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The last revision before TV support — an existing install would be at (at least)
# this point when the TV migration first runs.
_PRE_TV_REVISION = "41d427bd38e6"


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PLEX_MANAGER_DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    # Run alembic via the venv interpreter (-m) with fixed, test-controlled args.
    return subprocess.run(  # noqa: S603 — args are constants, not untrusted input
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tables_and_download_cols(db: Path) -> tuple[set[str], set[str]]:
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(downloads)")}
    finally:
        con.close()
    return tables, cols


def test_migration_chain_upgrades_head_and_downgrades_base(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    tables, dl_cols = _tables_and_download_cols(db)
    assert "season_requests" in tables
    assert {"season", "episodes_json"} <= dl_cols

    down = _alembic(db, "downgrade", "base")
    assert down.returncode == 0, down.stderr


def test_existing_install_upgrades_across_the_tv_revision(tmp_path: Path) -> None:
    """Regression for the P1 raised in Codex PR #22 review: an install already at the
    pre-TV revision must upgrade cleanly across the TV migration (its index steps
    reference `downloads.season` / `season_requests`, which the initial migration
    creates). Fails loudly here if the TV revision ever assumes an object no prior
    revision created."""
    db = tmp_path / "existing.db"
    stamp = _alembic(db, "upgrade", _PRE_TV_REVISION)
    assert stamp.returncode == 0, stamp.stderr

    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    tables, dl_cols = _tables_and_download_cols(db)
    assert "season_requests" in tables
    assert {"season", "episodes_json"} <= dl_cols
