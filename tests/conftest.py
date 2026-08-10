"""Suite-wide pytest classification used by the hybrid CI runner."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

CI_SERIAL_MODULES = frozenset(
    {
        "tests/adapters/filesystem/test_local.py",
        "tests/persistence/test_encryption_keyfile.py",
        "tests/services/test_eviction_service.py",
        "tests/services/test_health_service.py",
        "tests/services/test_import_service.py",
        "tests/services/test_log_capture_service.py",
        "tests/services/test_purge_service.py",
        "tests/services/test_update_coordination_service.py",
        "tests/updater/test_runner.py",
        "tests/web/test_settings.py",
        "tests/web/test_shutdown_wait.py",
    }
)


def is_ci_serial_nodeid(nodeid: str) -> bool:
    """Return whether a node belongs to a centrally classified serial module."""
    module = nodeid.partition("::")[0]
    return module in CI_SERIAL_MODULES


def pytest_collection_modifyitems(items: Sequence[pytest.Item]) -> None:
    """Mark whole sensitive modules; individual tests cannot drift between phases."""
    for item in items:
        if is_ci_serial_nodeid(item.nodeid):
            item.add_marker("ci_serial")
