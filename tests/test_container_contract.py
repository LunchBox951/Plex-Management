"""Static contracts for the production container and its CI smokes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WOLFI_BASE = (
    "cgr.dev/chainguard/wolfi-base:latest@"
    "sha256:003627df3c1e1bba0c4116afcddb314aca9594ee2328c7e876a8081a6c988b2e"
)
PYTHON_PACKAGE = "python-3.14=3.14.6-r4"
PIP_PACKAGE = "py3.14-pip=26.1.2-r1"
PIP_VERSION = "26.1.2"
FFMPEG_PACKAGE = "ffmpeg-8.1=8.1.2-r2"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _dockerfile_instructions() -> list[str]:
    dockerfile = _read("Dockerfile")
    logical_lines = dockerfile.replace("\\\n", " ").splitlines()
    return [
        line.strip() for line in logical_lines if line.strip() and not line.lstrip().startswith("#")
    ]


def test_dockerfile_pins_wolfi_and_exact_apk_packages() -> None:
    instructions = _dockerfile_instructions()
    from_lines = [line for line in instructions if line.startswith("FROM ")]
    run_lines = [line for line in instructions if line.startswith("RUN ")]

    assert sum(line.startswith(f"FROM {WOLFI_BASE} ") for line in from_lines) == 2
    assert sum(PYTHON_PACKAGE in line for line in run_lines) == 2
    assert sum(PIP_PACKAGE in line for line in run_lines) == 1
    assert sum(FFMPEG_PACKAGE in line for line in run_lines) == 1
    assert any(
        re.search(rf"\bpip install --upgrade pip=={re.escape(PIP_VERSION)}(?:\s|$)", line)
        for line in run_lines
    )


def test_dockerfile_preserves_runtime_contract_without_debian_tools() -> None:
    dockerfile = _read("Dockerfile")

    for forbidden in ("apt-get", "liblzma5", "useradd"):
        assert forbidden not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'CMD ["python", "-c",' in dockerfile


def test_container_workflow_runs_substantive_image_smokes_before_trivy() -> None:
    workflow = _read(".github/workflows/container.yml")
    runtime_smoke = "Runtime contract smoke"
    updater_smoke = "Root updater smoke"
    health_smoke = "Normal boot and health smoke"
    scan = "Scan image (Trivy"

    for name in (runtime_smoke, updater_smoke, health_smoke):
        assert f"- name: {name}" in workflow
    assert workflow.index(runtime_smoke) < workflow.index(scan)
    assert workflow.index(updater_smoke) < workflow.index(scan)
    assert workflow.index(health_smoke) < workflow.index(scan)

    assert "assert sys.version_info[:2] == (3, 14)" in workflow
    assert "assert os.geteuid() == 10001" in workflow
    assert 'ctypes.CDLL("libc.so.6")' in workflow
    assert 'pathlib.Path("/bin/sh").is_file()' in workflow
    assert 'shutil.which("ffprobe")' in workflow
    for module in (
        "cryptography",
        "asyncpg",
        "psycopg2",
        "uvloop",
        "watchfiles",
        "pydantic_core",
        "aiosqlite",
    ):
        assert module in workflow
    assert "docker run --rm --user 0:0 --entrypoint python" in workflow
    assert "import plex_manager.updater" in workflow
    assert "assert os.geteuid() == 0" in workflow
    assert "for attempt in $(seq 1 90)" in workflow
    assert "healthy) exit 0" in workflow
    assert "docker logs --tail 200" in workflow
    assert "docker rm -f" in workflow
