"""ffprobe-backed implementation of :class:`~plex_manager.ports.media_probe.MediaProbePort`.

Only compact, explicitly selected JSON fields cross the subprocess boundary.
The candidate path is one argv element after ``--`` and ``shell`` is disabled,
so a download-controlled filename is never interpreted as a command.  Error
messages deliberately omit both the candidate path and ffprobe's stderr because
either may contain request-derived text that is unsafe to persist in logs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from plex_manager.domain.plex_video import expected_probe_formats
from plex_manager.ports.media_probe import (
    MediaProbeError,
    MediaProbeResult,
    MediaProbeUnavailableError,
)

__all__ = ["FfprobeMediaProbe"]

_FFPROBE_NAME: Final = "ffprobe"
_PROBE_TIMEOUT_SECONDS: Final = 10.0
_SHOW_ENTRIES: Final = (
    "format=format_name:stream=codec_type,codec_name:stream_disposition=attached_pic"
)
_UNKNOWN_CODEC_NAMES: Final = frozenset({"", "unknown", "none", "n/a"})


def _mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    return None


def _sequence(value: object) -> Sequence[object] | None:
    if isinstance(value, (list, tuple)):
        return cast("Sequence[object]", value)
    return None


def _response(stdout: str) -> Mapping[str, object]:
    """Decode ffprobe JSON, distinguishing protocol failure from invalid media."""
    try:
        decoded: object = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MediaProbeUnavailableError("media probe returned malformed JSON") from exc
    payload = _mapping(decoded)
    if payload is None:
        raise MediaProbeUnavailableError("media probe returned an unexpected response shape")
    return payload


def _container_aliases(payload: Mapping[str, object]) -> tuple[str, ...]:
    format_fields = _mapping(payload.get("format"))
    if format_fields is None:
        raise MediaProbeUnavailableError("media probe response omitted the format object")
    format_name = format_fields.get("format_name")
    if format_name is None:
        return ()
    if not isinstance(format_name, str):
        raise MediaProbeUnavailableError("media probe returned an invalid container value")
    return tuple(alias for item in format_name.split(",") if (alias := item.strip().casefold()))


def _attached_picture(stream: Mapping[str, object]) -> bool:
    disposition = _mapping(stream.get("disposition"))
    if disposition is None:
        raise MediaProbeUnavailableError("media probe response omitted stream disposition")
    value = disposition.get("attached_pic")
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise MediaProbeUnavailableError("media probe returned an invalid stream disposition")


def _primary_video_codec(payload: Mapping[str, object]) -> str | None:
    streams = _sequence(payload.get("streams"))
    if streams is None:
        raise MediaProbeUnavailableError("media probe response omitted the stream list")
    for raw_stream in streams:
        stream = _mapping(raw_stream)
        if stream is None:
            raise MediaProbeUnavailableError("media probe returned an invalid stream entry")
        codec_type = stream.get("codec_type")
        if codec_type is not None and not isinstance(codec_type, str):
            raise MediaProbeUnavailableError("media probe returned an invalid stream type")
        if codec_type != "video" or _attached_picture(stream):
            continue
        codec_name = stream.get("codec_name")
        if codec_name is not None and not isinstance(codec_name, str):
            raise MediaProbeUnavailableError("media probe returned an invalid codec value")
        normalized = codec_name.strip().casefold() if codec_name is not None else ""
        if normalized not in _UNKNOWN_CODEC_NAMES:
            return normalized
    return None


class FfprobeMediaProbe:
    """Validate downloaded video candidates with the local ``ffprobe`` binary."""

    def probe(self, path: Path) -> MediaProbeResult:
        """Inspect ``path`` and require a suffix-matching primary video stream."""
        expected_formats = expected_probe_formats(os.fspath(path))
        if not expected_formats:
            raise MediaProbeError("candidate does not have a supported Plex video suffix")

        executable = shutil.which(_FFPROBE_NAME)
        if executable is None:
            raise MediaProbeUnavailableError("ffprobe is not installed")

        argv = [
            executable,
            "-v",
            "error",
            "-show_entries",
            _SHOW_ENTRIES,
            "-of",
            "json=compact=1",
            "--",
            os.fspath(path),
        ]
        try:
            completed = subprocess.run(  # noqa: S603 -- fixed executable; no shell
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaProbeUnavailableError("ffprobe timed out") from exc
        except OSError as exc:
            raise MediaProbeUnavailableError("ffprobe could not be executed") from exc

        if completed.returncode != 0:
            raise MediaProbeError("ffprobe rejected the candidate media file")

        payload = _response(completed.stdout)
        aliases = _container_aliases(payload)
        if not aliases:
            raise MediaProbeError("ffprobe did not identify a media container")
        container = next((alias for alias in aliases if alias in expected_formats), None)
        if container is None:
            raise MediaProbeError("candidate suffix does not match its detected container")

        video_codec = _primary_video_codec(payload)
        if video_codec is None:
            raise MediaProbeError("candidate has no known primary video stream")
        return MediaProbeResult(container=container, video_codec=video_codec)
