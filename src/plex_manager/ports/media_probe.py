"""MediaProbePort -- content-level validation for downloaded video files.

The importer already uses filename and manifest policy to decide which files may
enter a Plex library.  This boundary adds one deliberately narrower question:
does a candidate's *content* identify as the video container its suffix claims,
with bounded packet evidence from a real video stream?  The implementation is
synchronous because probing is a bounded local subprocess operation and the
import service already runs its blocking work off the event loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "MediaProbeError",
    "MediaProbePort",
    "MediaProbeResult",
    "MediaProbeUnavailableError",
]


class MediaProbeError(RuntimeError):
    """A deterministic rejection of candidate media content.

    The candidate may be corrupt, may not contain a decodable primary video
    stream, or may claim a filename suffix that disagrees with its detected
    container.  Callers may reject that candidate without treating the probing
    facility itself as unhealthy.
    """


class MediaProbeUnavailableError(MediaProbeError):
    """The probing facility could not produce a trustworthy verdict.

    Missing tooling, a bounded-timeout expiry, an operating-system failure, or
    an unexpected ffprobe response shape are retryable infrastructure failures,
    not evidence that the candidate itself is invalid.
    """


class MediaProbeResult(BaseModel):
    """An immutable snapshot of the accepted primary video stream.

    ``container`` is the first atomic ffprobe container alias that agrees with
    the path's suffix.  ``video_codec`` names the known, non-attached-picture
    video stream for which the probe observed bounded packet evidence.
    """

    model_config = ConfigDict(frozen=True)

    container: str
    video_codec: str


@runtime_checkable
class MediaProbePort(Protocol):
    """Validate a local candidate video file by inspecting its content."""

    def probe(self, path: Path, *, timeout_seconds: float | None = None) -> MediaProbeResult:
        """Return detected media facts or raise a typed probe error.

        ``timeout_seconds`` is an optional caller deadline. Implementations must
        cap their own normal timeout to that smaller remaining budget so probe
        work cannot outlive a whole-batch deadline.

        :raises MediaProbeError: when the candidate is deterministically not a
            supported, suffix-matching video file.
        :raises MediaProbeUnavailableError: when no trustworthy verdict can be
            produced because the probing facility failed.
        """
        raise NotImplementedError
