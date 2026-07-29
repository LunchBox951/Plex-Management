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
TZDATA_PACKAGE = "tzdata=2026c-r0"


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
    assert sum(TZDATA_PACKAGE in line for line in run_lines) == 1
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


def test_runtime_installs_timezone_data_and_limits_application_ownership() -> None:
    dockerfile = _read("Dockerfile")

    assert TZDATA_PACKAGE in dockerfile
    assert "chown 10001:10001 /app/data" in dockerfile
    assert "chown -R 10001:10001 /app" not in dockerfile


def _workflow_job(workflow: str, job: str, next_job: str) -> str:
    return workflow.split(f"\n  {job}:\n", 1)[1].split(f"\n  {next_job}:\n", 1)[0]


def test_publish_smokes_the_exact_candidate_before_publishing_tags() -> None:
    workflow = _read(".github/workflows/container.yml")
    publish = _workflow_job(workflow, "publish", "rescan")

    build = "Build candidate (load locally, no push)"
    runtime_smoke = "Runtime contract smoke"
    updater_smoke = "Root updater smoke"
    health_smoke = "Normal boot and health smoke"
    publish_tags = "Publish verified image tags"

    for name in (build, runtime_smoke, updater_smoke, health_smoke, publish_tags):
        assert f"- name: {name}" in publish
    assert "push: false" in publish
    assert "load: true" in publish
    assert publish.index(build) < publish.index(runtime_smoke) < publish.index(publish_tags)
    assert publish.index(build) < publish.index(updater_smoke) < publish.index(publish_tags)
    assert publish.index(build) < publish.index(health_smoke) < publish.index(publish_tags)
    assert ":candidate-${{ github.sha }}" in publish
    assert "steps.publish.outputs.digest" in publish


def test_workflow_runtime_smokes_enforce_timezone_and_ownership_contracts() -> None:
    workflow = _read(".github/workflows/container.yml")

    for job in (
        _workflow_job(workflow, "build", "publish"),
        _workflow_job(workflow, "publish", "rescan"),
    ):
        assert "from zoneinfo import ZoneInfo" in job
        assert 'ZoneInfo("UTC").key == "UTC"' in job
        assert 'ZoneInfo("America/New_York").key == "America/New_York"' in job
        assert 'pathlib.Path("/app/alembic.ini").stat().st_uid == 0' in job
        assert 'pathlib.Path("/app/migrations").stat().st_uid == 0' in job
        assert 'pathlib.Path("/app/data").stat().st_uid == 10001' in job
        assert 'pathlib.Path("/app/data/.write-probe")' in job


def test_container_workflow_runs_substantive_image_smokes_before_trivy() -> None:
    workflow = _read(".github/workflows/container.yml")
    build = _workflow_job(workflow, "build", "publish")
    runtime_smoke = "Runtime contract smoke"
    updater_smoke = "Root updater smoke"
    health_smoke = "Normal boot and health smoke"
    scan = "Scan image (Trivy"

    for name in (runtime_smoke, updater_smoke, health_smoke):
        assert f"- name: {name}" in build
    assert build.index(runtime_smoke) < build.index(scan)
    assert build.index(updater_smoke) < build.index(scan)
    assert build.index(health_smoke) < build.index(scan)

    assert "assert sys.version_info[:2] == (3, 14)" in build
    assert "assert os.geteuid() == 10001" in build
    assert 'ctypes.CDLL("libc.so.6")' in build
    assert 'pathlib.Path("/bin/sh").is_file()' in build
    assert 'shutil.which("ffprobe")' in build
    for module in (
        "cryptography",
        "asyncpg",
        "psycopg2",
        "uvloop",
        "watchfiles",
        "pydantic_core",
        "aiosqlite",
    ):
        assert module in build
    assert "docker run --rm --user 0:0 --entrypoint python" in build
    assert "import plex_manager.updater" in build
    assert "assert os.geteuid() == 0" in build
    assert "for attempt in $(seq 1 90)" in build
    assert "healthy) exit 0" in build
    assert "docker logs --tail 200" in build
    assert "docker rm -f" in build
