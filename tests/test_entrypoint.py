"""Static safety checks for the container entrypoint (mirrors test_promote_workflow.py)."""

from __future__ import annotations

from pathlib import Path

_ENTRYPOINT = Path("docker/entrypoint.sh")


def test_entrypoint_backs_up_before_migrating() -> None:
    text = _ENTRYPOINT.read_text()

    assert "set -e" in text
    backup_idx = text.index("python -m plex_manager.db_backup")
    migrate_idx = text.index("alembic upgrade head")
    exec_idx = text.index("exec python -m plex_manager")

    # The backup must run BEFORE the migration, and the exec must be last --
    # a regression here either skips the safety net or (worse) never actually
    # serves traffic.
    assert backup_idx < migrate_idx < exec_idx
