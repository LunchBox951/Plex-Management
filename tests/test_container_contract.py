"""Static contracts for the production container and its CI smokes."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
# The wolfi-base digest itself lives in the Dockerfile (its FROM lines are the
# single source of truth). The contract asserts the pin *format* and that the
# builder and runtime stages share one digest, so a Dependabot digest bump
# stays a one-file change that passes CI on its own.
WOLFI_BASE_FROM = re.compile(
    r"^FROM (cgr\.dev/chainguard/wolfi-base:latest@sha256:[0-9a-f]{64}) AS \S+$"
)
PYTHON_PACKAGE = "python-3.14=3.14.6-r4"
PIP_PACKAGE = "py3.14-pip=26.1.2-r1"
PIP_VERSION = "26.1.2"
FFMPEG_PACKAGE = "ffmpeg-8.1=8.1.2-r2"
TZDATA_PACKAGE = "tzdata=2026c-r0"
PUBLISH_SMOKES = (
    "Runtime contract smoke",
    "Root updater smoke",
    "Normal boot and health smoke",
)
CANDIDATE_TAG = "${{ steps.img.outputs.name }}:candidate-${{ github.sha }}"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _dockerfile_instructions() -> list[str]:
    dockerfile = _read("Dockerfile")
    logical_lines = dockerfile.replace("\\\n", " ").splitlines()
    return [
        line.strip() for line in logical_lines if line.strip() and not line.lstrip().startswith("#")
    ]


def _workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_read(".github/workflows/container.yml")))


def _workflow_events(workflow: dict[str, Any]) -> dict[str, Any]:
    """Read the workflow trigger mapping despite PyYAML's YAML 1.1 ``on`` quirk."""
    return cast(dict[str, Any], cast(dict[Any, Any], workflow)[True])


def _job(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    return cast(dict[str, Any], workflow["jobs"][name])


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], job["steps"])


def _step_named(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def _step_index(steps: list[dict[str, Any]], name: str) -> int:
    for index, step in enumerate(steps):
        if step.get("name") == name:
            return index
    raise AssertionError(f"no step named {name!r}")


def _build_actions(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        step for step in steps if str(step.get("uses", "")).startswith("docker/build-push-action@")
    ]


def _docker_command_pushes(command: list[str]) -> bool:
    """Identify a Docker command, not a word merely passed to another command."""
    leading_shell_tokens = {"!", "do", "if", "then"}
    docker_index = next((index for index, part in enumerate(command) if part == "docker"), None)
    if docker_index is None:
        return False

    prefix = command[:docker_index]
    if any(token not in leading_shell_tokens and "=" not in token for token in prefix):
        return False
    return any(
        argument == "push" or argument.startswith("--push")
        for argument in command[docker_index + 1 :]
    )


def _is_push_effecting_step(step: dict[str, Any]) -> bool:
    """Whether a step can publish an image to a registry.

    Treat every Docker CLI invocation with a ``push`` token (for example,
    ``docker image push``) or a ``--push`` option as push-effecting. Shell
    tokenization excludes comments and quoted strings passed to commands such as
    ``echo`` instead of matching their text as if it were a Docker invocation.
    """
    if step in _build_actions([step]):
        with_values = cast(dict[str, Any], step.get("with", {}))
        return with_values.get("push") is True

    # Join escaped newlines so a multi-line Docker command remains one command.
    run = str(step.get("run", "")).replace("\\\n", " ")
    for line in run.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        command: list[str] = []
        try:
            tokens = list(lexer)
        except ValueError:
            continue
        for token in tokens:
            if token in {";", "&&", "|", "||"}:
                if _docker_command_pushes(command):
                    return True
                command = []
            else:
                command.append(token)
        if _docker_command_pushes(command):
            return True
    return False


def test_dockerfile_pins_wolfi_and_exact_apk_packages() -> None:
    instructions = _dockerfile_instructions()
    from_lines = [line for line in instructions if line.startswith("FROM ")]
    run_lines = [line for line in instructions if line.startswith("RUN ")]

    wolfi_refs = [match.group(1) for line in from_lines if (match := WOLFI_BASE_FROM.match(line))]
    assert len(wolfi_refs) == 2, "builder and runtime must both build FROM digest-pinned wolfi-base"
    assert len(set(wolfi_refs)) == 1, "builder and runtime must share the same wolfi-base digest"
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
    instructions = _dockerfile_instructions()
    copy_lines = [line for line in instructions if line.startswith("COPY ")]
    add_lines = [line for line in instructions if line.startswith("ADD ")]
    chown_commands = [
        command
        for line in instructions
        for command in re.findall(r"(?:^|&&|;|\|\|)\s*(chown\s+[^;&|]+)", line)
    ]
    protected_copy_lines = [
        line
        for line in copy_lines
        if any(path in line.split() for path in ("alembic.ini", "migrations"))
    ]

    assert TZDATA_PACKAGE in "\n".join(instructions)
    assert chown_commands == ["chown 10001:10001 /app/data"], (
        "only the writable data directory may be chowned"
    )
    assert not add_lines, (
        "ADD is forbidden: its URL, auto-extract, and --chown semantics are unnecessary; "
        "use COPY for image assets"
    )
    assert protected_copy_lines, "expected COPY instructions for migration assets"
    assert all("--chown" not in line for line in protected_copy_lines), (
        "migration assets must remain root-owned"
    )


def test_publish_smokes_the_exact_candidate_before_publishing_tags() -> None:
    publish_steps = _steps(_job(_workflow(), "publish"))
    candidate_build = _step_named(publish_steps, "Build candidate (load locally, no push)")
    build_actions = _build_actions(publish_steps)
    smoke_indices = [_step_index(publish_steps, name) for name in PUBLISH_SMOKES]
    publish_index = _step_index(publish_steps, "Publish verified image tags")

    assert len(build_actions) == 1, "publish must build exactly one local candidate"
    assert build_actions == [candidate_build]
    build_with = cast(dict[str, Any], candidate_build["with"])
    assert build_with.get("push") is False
    assert build_with.get("load") is True
    assert build_with.get("provenance") is False
    assert build_with.get("tags") == CANDIDATE_TAG

    for name in PUBLISH_SMOKES:
        smoke_env = cast(dict[str, Any], _step_named(publish_steps, name)["env"])
        assert smoke_env.get("IMAGE") == CANDIDATE_TAG, f"{name} must smoke the candidate"

    assert max(smoke_indices) < publish_index
    push_effects = [
        (index, step.get("name", "<unnamed>"))
        for index, step in enumerate(publish_steps)
        if _is_push_effecting_step(step)
    ]
    assert push_effects, "publish must eventually push its verified tags"
    assert all(index > max(smoke_indices) for index, _ in push_effects), (
        f"publish effect before all smokes: {push_effects}"
    )


def test_publish_workflow_gates_writes_and_verifies_tag_order_remotely() -> None:
    workflow = _workflow()
    events = _workflow_events(workflow)
    publish = _job(workflow, "publish")
    publish_steps = _steps(publish)
    publish_step = _step_named(publish_steps, "Publish verified image tags")
    publish_run = str(publish_step["run"])

    assert events["push"]["branches"] == ["main"]
    assert publish.get("if") == "github.event_name == 'push'"

    write_jobs = [
        name
        for name, job in cast(dict[str, dict[str, Any]], workflow["jobs"]).items()
        if cast(dict[str, Any], job.get("permissions", {})).get("packages") == "write"
    ]
    assert write_jobs == ["publish"], "only the push-gated publish job may write GHCR"

    immutable_push = 'docker push "$immutable"'
    moving_push = 'docker push "$moving"'
    immutable_push_index = publish_run.index(immutable_push)
    moving_push_index = publish_run.index(moving_push)
    digest_lookup = 'docker buildx imagetools inspect "$immutable"'
    moving_digest_lookup = 'docker buildx imagetools inspect "$moving"'
    digest_lookup_index = publish_run.index(digest_lookup)
    moving_digest_lookup_index = publish_run.index(moving_digest_lookup)

    assert immutable_push_index < moving_push_index
    assert moving_push_index < digest_lookup_index < moving_digest_lookup_index
    assert publish_run.index('test "$moving_digest" = "$digest"') > moving_digest_lookup_index


def test_workflow_runtime_smokes_enforce_timezone_and_ownership_contracts() -> None:
    workflow = _workflow()

    for job_name in ("build", "publish"):
        steps = _steps(_job(workflow, job_name))
        runtime_smoke = str(_step_named(steps, "Runtime contract smoke").get("run", ""))
        assert "from zoneinfo import ZoneInfo" in runtime_smoke
        assert 'ZoneInfo("UTC").key == "UTC"' in runtime_smoke
        assert 'ZoneInfo("America/New_York").key == "America/New_York"' in runtime_smoke
        assert 'pathlib.Path("/app/alembic.ini").stat().st_uid == 0' in runtime_smoke
        assert 'pathlib.Path("/app/migrations").stat().st_uid == 0' in runtime_smoke
        assert 'pathlib.Path("/app/data").stat().st_uid == 10001' in runtime_smoke
        assert 'pathlib.Path("/app/data/.write-probe")' in runtime_smoke


def test_container_workflow_runs_substantive_image_smokes_before_trivy() -> None:
    build_steps = _steps(_job(_workflow(), "build"))
    runtime_smoke = _step_named(build_steps, "Runtime contract smoke")
    updater_smoke = _step_named(build_steps, "Root updater smoke")
    health_smoke = _step_named(build_steps, "Normal boot and health smoke")
    scan_index = next(
        index
        for index, step in enumerate(build_steps)
        if str(step.get("name", "")).startswith("Scan image (Trivy")
    )

    for smoke_name in PUBLISH_SMOKES:
        assert _step_index(build_steps, smoke_name) < scan_index

    runtime_run = str(runtime_smoke["run"])
    updater_run = str(updater_smoke["run"])
    health_run = str(health_smoke["run"])
    assert "assert sys.version_info[:2] == (3, 14)" in runtime_run
    assert "assert os.geteuid() == 10001" in runtime_run
    assert 'ctypes.CDLL("libc.so.6")' in runtime_run
    assert 'pathlib.Path("/bin/sh").is_file()' in runtime_run
    assert 'shutil.which("ffprobe")' in runtime_run
    for module in (
        "cryptography",
        "asyncpg",
        "psycopg2",
        "uvloop",
        "watchfiles",
        "pydantic_core",
        "aiosqlite",
    ):
        assert module in runtime_run
    assert "docker run --rm --user 0:0 --entrypoint python" in updater_run
    assert "import plex_manager.updater" in updater_run
    assert "assert os.geteuid() == 0" in updater_run
    assert "for attempt in $(seq 1 90)" in health_run
    assert "healthy) exit 0" in health_run
    assert "docker logs --tail 200" in health_run
    assert "docker rm -f" in health_run
