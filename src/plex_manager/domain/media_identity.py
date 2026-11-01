"""Conservative identity conflicts; missing runtime is not evidence of bad media."""

import math

REVIEWABLE_REQUEST_STATUSES = frozenset(
    {"pending", "searching", "no_acceptable_release", "downloading", "import_blocked"}
)


def movie_runtime_conflict(
    actual_seconds: float | None, expected_minutes: int | None
) -> str | None:
    if (
        actual_seconds is None
        or not math.isfinite(actual_seconds)
        or actual_seconds <= 0
        or expected_minutes is None
        or expected_minutes <= 0
    ):
        return None
    actual_minutes = actual_seconds / 60
    difference = abs(actual_minutes - expected_minutes)
    if difference < 30 or difference / expected_minutes < 0.4:
        return None
    return (
        f"Runtime differs substantially: file {actual_minutes:.1f} minutes, "
        f"expected about {expected_minutes} minutes. This may be another movie or an "
        "alternate cut. Searching for another release; you can import this file anyway."
    )
