"""Run the CI pytest population in parallel and serial coverage phases."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable

Command = tuple[str, ...]
CommandRunner = Callable[[Command], int]
Clock = Callable[[], float]


def ci_commands(python: str = sys.executable) -> tuple[tuple[str, Command], ...]:
    return (
        ("coverage-erase", (python, "-m", "coverage", "erase")),
        (
            "parallel",
            (
                python,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-m",
                "not ci_serial",
                "-n",
                "3",
                "--dist=worksteal",
                "--cov=plex_manager",
                "--cov-report=",
                "--durations=50",
            ),
        ),
        (
            "serial",
            (
                python,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-m",
                "ci_serial",
                "--cov=plex_manager",
                "--cov-append",
                "--cov-report=",
                "--durations=50",
            ),
        ),
        (
            "coverage-report",
            (python, "-m", "coverage", "report", "--show-missing"),
        ),
    )


def _execute(command: Command) -> int:
    return subprocess.run(command, check=False).returncode  # noqa: S603


def run_ci_tests(*, runner: CommandRunner = _execute, clock: Clock = time.monotonic) -> int:
    """Execute every phase and fail when any pytest or coverage phase fails."""
    failed = False
    for name, command in ci_commands():
        print(f"::group::CI test phase: {name}", flush=True)
        started = clock()
        return_code = runner(command)
        elapsed = clock() - started
        print(
            f"ci-test phase={name} exit_code={return_code} duration_seconds={elapsed:.3f}",
            flush=True,
        )
        print("::endgroup::", flush=True)
        failed = failed or return_code != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(run_ci_tests())
