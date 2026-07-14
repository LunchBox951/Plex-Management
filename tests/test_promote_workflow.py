"""Static safety checks for the manual image promotion workflow."""

from pathlib import Path

_WORKFLOW = Path(".github/workflows/promote.yml")
_CONTAINER_WORKFLOW = Path(".github/workflows/container.yml")


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
