import pytest

from plex_manager.domain.media_identity import movie_runtime_conflict


@pytest.mark.parametrize(
    "actual, expected",
    [
        (None, 173),
        (5163.456, None),
        (0, 173),
        (float("nan"), 173),
        (float("inf"), 173),
        (194 * 60, 144),
        (169.1 * 60, 169),
    ],
)
def test_missing_runtime_and_ordinary_edition_differences_do_not_block(
    actual: float | None,
    expected: int | None,
) -> None:
    assert movie_runtime_conflict(actual, expected) is None


def test_half_length_odyssey_is_flagged_for_review() -> None:
    reason = movie_runtime_conflict(5163.456, 173)
    assert reason is not None
    assert "86.1 minutes" in reason
    assert "173 minutes" in reason
    assert "import this file anyway" in reason
