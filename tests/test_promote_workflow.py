"""Static safety checks for the manual image promotion workflow."""

from pathlib import Path

_WORKFLOW = Path(".github/workflows/promote.yml")


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
