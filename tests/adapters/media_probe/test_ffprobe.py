"""Unit tests for the ffprobe adapter (the real binary is never invoked)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

import plex_manager.adapters.media_probe.ffprobe as ffprobe_module
from plex_manager.adapters.media_probe import FfprobeMediaProbe
from plex_manager.ports.media_probe import (
    MediaProbeError,
    MediaProbePort,
    MediaProbeUnavailableError,
)


def _payload(
    *,
    format_name: str = "matroska,webm",
    streams: list[object] | None = None,
) -> dict[str, object]:
    return {
        "format": {"format_name": format_name},
        "streams": streams
        if streams is not None
        else [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"attached_pic": 0},
            }
        ],
    }


def _completed(
    payload: object,
    *,
    returncode: int = 0,
    stdout: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["/usr/bin/ffprobe"],
        returncode=returncode,
        stdout=json.dumps(payload) if stdout is None else stdout,
        stderr="candidate-derived stderr is deliberately ignored",
    )


@pytest.fixture(autouse=True)
def installed_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    def find_ffprobe(_name: str) -> str:
        return "/usr/bin/ffprobe"

    monkeypatch.setattr(ffprobe_module.shutil, "which", find_ffprobe)


def _stub_run(
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[str],
) -> list[tuple[list[str], dict[str, object]]]:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return completed

    monkeypatch.setattr(ffprobe_module.subprocess, "run", run)
    return calls


def test_probe_uses_bounded_no_shell_argv_and_compact_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_run(monkeypatch, _completed(_payload(format_name="mov,mp4,m4a,3gp,3g2,mj2")))
    path = Path("/downloads/Movie; touch never.mp4")

    result = FfprobeMediaProbe().probe(path)

    assert result.container == "mov"
    assert result.video_codec == "h264"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "/usr/bin/ffprobe"
    assert argv[-2:] == ["--", str(path)]
    assert "json=compact=1" in argv
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 10.0
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_probe_accepts_real_video_after_attached_picture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[object] = [
        {
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
        },
        {"codec_type": "audio", "codec_name": "aac", "disposition": {"attached_pic": 0}},
        {"codec_type": "video", "codec_name": "hevc", "disposition": {"attached_pic": 0}},
    ]
    _stub_run(monkeypatch, _completed(_payload(streams=streams)))

    result = FfprobeMediaProbe().probe(Path("movie.MKV"))

    assert result.container == "matroska"
    assert result.video_codec == "hevc"


@pytest.mark.parametrize(
    "streams",
    [
        [{"codec_type": "audio", "codec_name": "aac", "disposition": {"attached_pic": 0}}],
        [
            {
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 1},
            }
        ],
        [
            {
                "codec_type": "video",
                "codec_name": "unknown",
                "disposition": {"attached_pic": 0},
            }
        ],
    ],
)
def test_probe_rejects_without_known_non_attached_picture_video(
    monkeypatch: pytest.MonkeyPatch,
    streams: list[object],
) -> None:
    _stub_run(monkeypatch, _completed(_payload(streams=streams)))

    with pytest.raises(MediaProbeError, match="no known primary video stream"):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


def test_probe_rejects_suffix_container_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _completed(_payload(format_name="avi")))

    with pytest.raises(MediaProbeError, match="suffix does not match"):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


def test_probe_rejects_unsupported_suffix_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return _completed(_payload())

    monkeypatch.setattr(ffprobe_module.subprocess, "run", run)

    with pytest.raises(MediaProbeError, match="supported Plex video suffix"):
        FfprobeMediaProbe().probe(Path("movie.exe"))
    assert not called


def test_nonzero_ffprobe_exit_is_deterministic_media_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, _completed({}, returncode=1))

    with pytest.raises(MediaProbeError, match="rejected") as raised:
        FfprobeMediaProbe().probe(Path("broken.mkv"))
    assert not isinstance(raised.value, MediaProbeUnavailableError)


@pytest.mark.parametrize(
    "completed",
    [
        _completed({}, stdout="not json"),
        _completed([]),
        _completed({"format": {"format_name": 42}, "streams": []}),
        _completed({"format": {"format_name": "matroska"}, "streams": "not-a-list"}),
        _completed(
            _payload(streams=[{"codec_type": "video", "codec_name": "h264", "disposition": {}}])
        ),
    ],
)
def test_malformed_ffprobe_protocol_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[str],
) -> None:
    _stub_run(monkeypatch, completed)

    with pytest.raises(MediaProbeUnavailableError):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


def test_missing_ffprobe_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_ffprobe(_name: str) -> None:
        return None

    monkeypatch.setattr(ffprobe_module.shutil, "which", missing_ffprobe)

    with pytest.raises(MediaProbeUnavailableError, match="not installed"):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=10.0),
        OSError("exec failed"),
    ],
)
def test_execution_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(ffprobe_module.subprocess, "run", run)

    with pytest.raises(MediaProbeUnavailableError):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


def test_probe_result_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _completed(_payload()))
    result = FfprobeMediaProbe().probe(Path("movie.mkv"))

    with pytest.raises(ValidationError):
        result.container = "avi"  # pyright: ignore[reportAttributeAccessIssue]


def test_adapter_satisfies_sync_media_probe_protocol() -> None:
    probe: Callable[[Path], object] = FfprobeMediaProbe().probe
    assert callable(probe)
    assert isinstance(FfprobeMediaProbe(), MediaProbePort)
