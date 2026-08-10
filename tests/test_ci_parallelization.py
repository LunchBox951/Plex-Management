"""Contracts for the hybrid parallel/serial CI test design."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import yaml

from scripts.ci_test import Command, ci_commands, run_ci_tests
from tests.conftest import CI_SERIAL_MODULES, is_ci_serial_nodeid

_CI_WORKFLOW = Path(".github/workflows/ci.yml")
_MAKEFILE = Path("Makefile")
_PYPROJECT = Path("pyproject.toml")


def test_ci_serial_classification_is_module_wide_and_exact() -> None:
    assert {
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
    } == CI_SERIAL_MODULES
    for module in CI_SERIAL_MODULES:
        assert is_ci_serial_nodeid(f"{module}::test_one")
        assert is_ci_serial_nodeid(f"{module}::TestGroup::test_two")
    assert not is_ci_serial_nodeid("tests/domain/test_eviction.py::test_policy")


def test_ci_runner_preserves_and_combines_coverage() -> None:
    commands = dict(ci_commands("python"))

    assert commands["coverage-erase"] == ("python", "-m", "coverage", "erase")
    assert commands["parallel"][3:5] == ("-o", "addopts=")
    assert commands["parallel"][5:7] == ("-m", "not ci_serial")
    assert commands["parallel"][7:10] == ("-n", "3", "--dist=worksteal")
    assert "--cov=plex_manager" in commands["parallel"]
    assert "--cov-report=" in commands["parallel"]
    assert "--cov-append" not in commands["parallel"]
    assert commands["serial"][3:5] == ("-o", "addopts=")
    assert commands["serial"][5:7] == ("-m", "ci_serial")
    assert "--cov=plex_manager" in commands["serial"]
    assert "--cov-report=" in commands["serial"]
    assert "-n" not in commands["serial"]
    assert "--cov-append" in commands["serial"]
    assert commands["coverage-report"] == (
        "python",
        "-m",
        "coverage",
        "report",
        "--show-missing",
    )
    assert "--durations=50" in commands["parallel"]
    assert "--durations=50" in commands["serial"]


def test_ci_runner_always_executes_every_phase_and_aggregates_failure() -> None:
    seen: list[Command] = []
    return_codes = iter((1, 2, 0, 3))

    def runner(command: Command) -> int:
        seen.append(command)
        return next(return_codes)

    ticks: Iterator[float] = iter(float(value) for value in range(8))
    assert run_ci_tests(runner=runner, clock=lambda: next(ticks)) == 1
    assert seen == [command for _name, command in ci_commands()]


def test_ci_runner_succeeds_only_when_every_phase_succeeds() -> None:
    ticks: Iterator[float] = iter(float(value) for value in range(8))
    assert run_ci_tests(runner=lambda _command: 0, clock=lambda: next(ticks)) == 0


def test_ci_workflow_preserves_required_gate_identities_and_topology() -> None:
    workflow = cast(dict[str, Any], yaml.safe_load(_CI_WORKFLOW.read_text()))
    jobs = cast(dict[str, Any], workflow["jobs"])

    assert set(jobs) == {"quality", "tests-py314", "frontend"}
    assert jobs["quality"]["runs-on"] == "ubuntu-latest"
    assert jobs["tests-py314"]["runs-on"] == "ubuntu-latest"
    assert jobs["frontend"]["runs-on"] == "ubuntu-latest"

    quality_steps = cast(list[dict[str, Any]], jobs["quality"]["steps"])
    py314_steps = cast(list[dict[str, Any]], jobs["tests-py314"]["steps"])
    assert [step["name"] for step in quality_steps if "name" in step] == [
        "Install",
        "OpenAPI contract is fresh",
        "Lint (ruff)",
        "Format check (ruff)",
        "Type check (pyright --strict)",
        "Tests",
    ]
    assert next(step for step in quality_steps if step.get("name") == "Tests")["run"] == (
        "make test-ci"
    )
    assert next(step for step in py314_steps if step.get("name") == "Tests")["run"] == (
        "make test-ci"
    )


def test_make_test_stays_serial_while_ci_uses_hybrid_runner() -> None:
    makefile = _MAKEFILE.read_text()
    assert "test: ## Run the test suite with coverage\n\tpytest\n" in makefile
    assert "test-ci: ## Run CI tests in parallel, then timing-sensitive tests serially" in makefile
    assert "\tpython -m scripts.ci_test\n" in makefile


def test_xdist_and_marker_are_pinned_in_project_configuration() -> None:
    pyproject = _PYPROJECT.read_text()
    assert '"pytest-xdist==3.8.0"' in pyproject
    assert '"ci_serial: timing- or thread-sensitive tests run serially in CI"' in pyproject
