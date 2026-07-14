"""Static safety checks for the manual image promotion workflow."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOW = Path(".github/workflows/promote.yml")
_CONTAINER_WORKFLOW = Path(".github/workflows/container.yml")


def _promote_steps() -> list[dict[str, Any]]:
    doc = cast(dict[str, Any], yaml.safe_load(_WORKFLOW.read_text()))
    steps = doc["jobs"]["promote"]["steps"]
    return cast("list[dict[str, Any]]", steps)


def _run_steps() -> list[dict[str, Any]]:
    """Steps that carry an inline shell script (a ``run:`` key)."""
    return [s for s in _promote_steps() if "run" in s]


def _step_named(needle: str) -> dict[str, Any]:
    for step in _promote_steps():
        if needle in str(step.get("name", "")):
            return step
    raise AssertionError(f"no promote step whose name contains {needle!r}")


def test_promote_workflow_validates_inputs_without_line_based_grep() -> None:
    text = _WORKFLOW.read_text()

    assert "grep -Eq" not in text
    assert "contains control characters" in text
    assert '[[ ! "$SOURCE_TAG" =~ ^edge-[A-Za-z0-9._-]+$ ]]' in text
    assert '[[ ! "$VERSION" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ ]]' in text


def test_promote_workflow_builds_docker_tags_as_argv_array() -> None:
    text = _WORKFLOW.read_text()

    assert 'tags=(--tag "${IMAGE}:stable")' in text
    assert 'docker buildx imagetools create "${tags[@]}" "${IMAGE}:${SOURCE_TAG}"' in text
    assert "docker buildx imagetools create $tags" not in text


def test_promote_workflow_gates_on_baked_image_version_label() -> None:
    """#231 — the promoted edge-<sha> can predate the current main tree, so the
    gate must read the version baked into the IMAGE (an OCI label), never the
    checked-out repo's __init__.py, and it must run before the re-tag step."""
    text = _WORKFLOW.read_text()

    gate_idx = text.index("Gate: promoted version must match the image's baked label")
    promote_idx = text.index('docker buildx imagetools create "${tags[@]}"')
    assert gate_idx < promote_idx, "the gate must run before the imagetools create re-tag"

    # Reads the label baked into the image at build time, not the repo checkout.
    assert 'docker buildx imagetools inspect "${IMAGE}:${SOURCE_TAG}"' in text, (
        "gate must inspect the source image, not the checked-out tree"
    )
    assert "--format '{{index .Image.Config.Labels \"org.opencontainers.image.version\"}}'" in text

    # Only meaningful when the operator actually supplied a version to check
    # against; a blank version (":stable" re-tag only) has nothing to compare.
    assert "if: inputs.version != ''" in text

    # Fail closed on every failure mode: unreadable manifest, missing label,
    # and value mismatch each get their own hard-exit branch.
    assert "if ! baked_version=$(docker buildx imagetools inspect" in text
    assert 'if [ -z "$baked_version" ]' in text
    assert 'if [ "$baked_version" != "$VERSION" ]' in text
    assert text.count("exit 1") >= 6  # 4 input-validation branches + 3 gate branches, at least


def test_container_workflow_stamps_version_label_from_init_py() -> None:
    """Sibling to the promote-workflow gate: the label the gate trusts has to
    actually be stamped at build time from the single source of truth
    (src/plex_manager/__init__.py), and must win over metadata-action's own
    auto-derived org.opencontainers.image.version (which would just be the
    literal tag name "edge", not a real version)."""
    text = _CONTAINER_WORKFLOW.read_text()

    assert "grep -oP" in text
    assert "src/plex_manager/__init__.py" in text
    assert 'echo "value=$version" >> "$GITHUB_OUTPUT"' in text

    # Our stamped label is listed AFTER metadata-action's own labels output so
    # it wins on the duplicate org.opencontainers.image.version key.
    labels_block = text[text.index("labels: |") : text.index("labels: |") + 200]
    meta_pos = labels_block.index("steps.meta.outputs.labels")
    version_label = "org.opencontainers.image.version=${{ steps.version.outputs.value }}"
    version_pos = labels_block.index(version_label)
    assert meta_pos < version_pos


def test_every_inline_shell_step_is_strict() -> None:
    """#348 — every ``run:`` step must open with ``set -euo pipefail`` so a
    future edit inserting a fallible command above the last one fails the run
    instead of silently promoting. No exceptions: the whole file is fail-fast."""
    offenders = [
        s.get("name", "<unnamed>")
        for s in _run_steps()
        if s["run"].lstrip().splitlines()[0].strip() != "set -euo pipefail"
    ]
    assert not offenders, f"run steps missing 'set -euo pipefail' as line 1: {offenders}"


def test_third_party_actions_are_pinned_to_a_commit_sha() -> None:
    """#352 — supply-chain: buildx/login must be pinned to a full 40-char commit
    SHA (not a floating ``@v4`` that a tag-move could repoint mid-release)."""
    uses = [str(s["uses"]) for s in _promote_steps() if "uses" in s]
    assert uses, "expected at least one `uses:` action in the promote job"
    for ref in uses:
        pin = ref.split("@", 1)[1].split()[0] if "@" in ref else ""
        assert re.fullmatch(r"[0-9a-f]{40}", pin), f"action not SHA-pinned: {ref!r}"


# --- gate logic executed for real (buildx stubbed) ------------------------------
#
# The promote gate had only string assertions; #352 flagged that its shell has
# never actually run. Here we extract the gate step's exact `run:` script from the
# YAML and execute it under bash with `docker` shadowed by a shell function, so
# the real branching (unreadable / empty label / mismatch / match) is exercised.

_GATE_SCENARIOS = [
    # id, stub_version, stub_fail, version_input, want_rc, want_substr
    ("match", "1.0.0", "0", "1.0.0", 0, "OK:"),
    ("mismatch", "0.0.0", "0", "1.0.0", 1, "Version mismatch"),
    ("empty_label", "", "0", "1.0.0", 1, "has no org.opencontainers.image.version label"),
    ("unreadable", "", "1", "1.0.0", 1, "Could not read the manifest"),
]


@pytest.mark.parametrize(
    ("stub_version", "stub_fail", "version_input", "want_rc", "want_substr"),
    [s[1:] for s in _GATE_SCENARIOS],
    ids=[s[0] for s in _GATE_SCENARIOS],
)
def test_gate_shell_fails_closed(
    stub_version: str,
    stub_fail: str,
    version_input: str,
    want_rc: int,
    want_substr: str,
) -> None:
    gate_run = str(_step_named("Gate:")["run"])
    # Shadow the buildx CLI with a function driven by env: it emulates
    # `docker buildx imagetools inspect ... --format ...` printing the baked
    # label (STUB_VERSION) or failing outright (STUB_FAIL=1, e.g. bad tag /
    # registry error). Defaults keep it safe under the gate's own `set -u`.
    stub = (
        'docker() { if [ "${STUB_FAIL:-0}" = "1" ]; then return 1; fi; '
        'printf "%s" "${STUB_VERSION-}"; }\n'
    )
    script = stub + gate_run
    proc = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        env={
            "PATH": "/usr/bin:/bin",
            "IMAGE": "ghcr.io/lunchbox951/plex-manager",
            "SOURCE_TAG": "edge-abc1234",
            "VERSION": version_input,
            "STUB_VERSION": stub_version,
            "STUB_FAIL": stub_fail,
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == want_rc, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert want_substr in proc.stdout, f"stdout={proc.stdout!r}"
