"""Static contracts for the production container and its CI smokes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WOLFI_BASE = (
    "cgr.dev/chainguard/wolfi-base:latest@"
    "sha256:003627df3c1e1bba0c4116afcddb314aca9594ee2328c7e876a8081a6c988b2e"
)
PYTHON_PACKAGE = "python-3.14=3.14.6-r4"
PIP_PACKAGE = "py3.14-pip=26.1.2-r1"
FFMPEG_PACKAGE = "ffmpeg-8.1=8.1.2-r2"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_dockerfile_pins_wolfi_and_exact_apk_packages() -> None:
    dockerfile = _read("Dockerfile")

    assert dockerfile.count(f"FROM {WOLFI_BASE}") == 2
    assert PYTHON_PACKAGE in dockerfile
    assert PIP_PACKAGE in dockerfile
    assert FFMPEG_PACKAGE in dockerfile
    assert dockerfile.count(PYTHON_PACKAGE) == 2


def test_dockerfile_preserves_runtime_contract_without_debian_tools() -> None:
    dockerfile = _read("Dockerfile")

    for forbidden in ("apt-get", "liblzma5", "useradd"):
        assert forbidden not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'CMD ["python", "-c",' in dockerfile


def test_container_workflow_runs_all_image_smokes_before_trivy() -> None:
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
