"""Unit tests for the ffprobe adapter, plus optional real-binary contract smokes."""

from __future__ import annotations

import json
import shutil
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
    packets: list[object] | None = None,
) -> dict[str, object]:
    return {
        "format": {"format_name": format_name},
        "streams": streams
        if streams is not None
        else [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"attached_pic": 0},
            }
        ],
        "packets": packets if packets is not None else [{"stream_index": 0}],
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


def _installed_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    def find_ffprobe(_name: str) -> str:
        return "/usr/bin/ffprobe"

    monkeypatch.setattr(ffprobe_module.shutil, "which", find_ffprobe)


def _stub_run(
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[str],
) -> list[tuple[list[str], dict[str, object]]]:
    _installed_ffprobe(monkeypatch)
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
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert argv[argv.index("-select_streams") + 1] == "V"
    assert argv[argv.index("-read_intervals") + 1] == "%+#32"
    assert "-show_format" not in argv
    assert "-show_streams" not in argv
    assert "-show_packets" not in argv
    show_entries = argv[argv.index("-show_entries") + 1]
    assert "stream=index,codec_type,codec_name" in show_entries
    assert "packet=stream_index" in show_entries
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 10.0
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in kwargs
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "strict"


def test_probe_caps_its_timeout_to_the_callers_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_run(monkeypatch, _completed(_payload()))
    probe = FfprobeMediaProbe()

    probe.probe(Path("first.mkv"), timeout_seconds=2.5)
    probe.probe(Path("second.mkv"), timeout_seconds=30.0)

    assert [kwargs["timeout"] for _argv, kwargs in calls] == [2.5, 10.0]


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0, float("nan"), float("inf")])
def test_probe_rejects_an_expired_or_invalid_caller_deadline(
    timeout_seconds: float,
) -> None:
    with pytest.raises(MediaProbeUnavailableError, match="deadline expired"):
        FfprobeMediaProbe().probe(Path("movie.mkv"), timeout_seconds=timeout_seconds)


def test_probe_accepts_real_video_after_attached_picture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[object] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        {
            "index": 2,
            "codec_type": "video",
            "codec_name": "hevc",
            "disposition": {"attached_pic": 0},
        },
    ]
    _stub_run(
        monkeypatch,
        _completed(_payload(streams=streams, packets=[{"stream_index": 2}])),
    )

    result = FfprobeMediaProbe().probe(Path("movie.MKV"))

    assert result.container == "matroska"
    assert result.video_codec == "hevc"


def test_probe_accepts_when_later_bounded_packet_maps_to_known_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[object] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "unknown",
            "disposition": {"attached_pic": 0},
        },
        {
            "index": 1,
            "codec_type": "video",
            "codec_name": "hevc",
            "disposition": {"attached_pic": 0},
        },
    ]
    packets: list[object] = [{"stream_index": 0}, {"stream_index": 1}]
    _stub_run(monkeypatch, _completed(_payload(streams=streams, packets=packets)))

    result = FfprobeMediaProbe().probe(Path("movie.mkv"))

    assert result.video_codec == "hevc"


@pytest.mark.parametrize(
    ("streams", "packet_index"),
    [
        ([{"index": 3, "codec_type": "audio", "codec_name": "aac"}], 3),
        [
            [
                {
                    "index": 4,
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "disposition": {"attached_pic": 1},
                }
            ],
            4,
        ],
        [
            [
                {
                    "index": 5,
                    "codec_type": "video",
                    "codec_name": "unknown",
                    "disposition": {"attached_pic": 0},
                }
            ],
            5,
        ],
    ],
)
def test_probe_rejects_packet_without_known_non_attached_picture_video(
    monkeypatch: pytest.MonkeyPatch,
    streams: list[object],
    packet_index: int,
) -> None:
    _stub_run(
        monkeypatch,
        _completed(_payload(streams=streams, packets=[{"stream_index": packet_index}])),
    )

    with pytest.raises(MediaProbeError, match="no readable packet"):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


def test_probe_rejects_known_video_stream_with_zero_packets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, _completed(_payload(packets=[])))

    with pytest.raises(MediaProbeError, match="no readable packet") as raised:
        FfprobeMediaProbe().probe(Path("movie.mkv"))
    assert not isinstance(raised.value, MediaProbeUnavailableError)


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


def test_signal_terminated_ffprobe_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _completed({}, returncode=-9))

    with pytest.raises(MediaProbeUnavailableError, match="without a media verdict"):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


@pytest.mark.parametrize(
    "completed",
    [
        _completed({}, stdout="not json"),
        _completed([]),
        _completed({"format": {"format_name": 42}, "streams": []}),
        _completed(
            {
                "format": {"format_name": "matroska"},
                "streams": "not-a-list",
                "packets": [],
            }
        ),
        _completed(
            _payload(
                streams=[
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "disposition": {},
                    }
                ]
            )
        ),
        _completed({"format": {"format_name": "matroska"}, "streams": []}),
        _completed(
            {
                "format": {"format_name": "matroska"},
                "streams": [],
                "packets": "not-a-list",
            }
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


@pytest.mark.parametrize(
    "streams",
    [
        [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"attached_pic": 0},
            }
        ],
        [
            {
                "index": True,
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"attached_pic": 0},
            }
        ],
        [
            {
                "index": -1,
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"attached_pic": 0},
            }
        ],
        [
            {
                "index": 7,
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"attached_pic": 0},
            },
            {
                "index": 7,
                "codec_type": "video",
                "codec_name": "hevc",
                "disposition": {"attached_pic": 0},
            },
        ],
    ],
)
def test_invalid_or_duplicate_stream_index_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    streams: list[object],
) -> None:
    _stub_run(monkeypatch, _completed(_payload(streams=streams)))

    with pytest.raises(MediaProbeUnavailableError):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


@pytest.mark.parametrize(
    "packets",
    [
        [{}],
        [{"stream_index": True}],
        [{"stream_index": -1}],
        [{"stream_index": 99}],
        [{"stream_index": 0}] * 33,
        ["not-an-object"],
    ],
)
def test_invalid_packet_or_stream_reference_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    packets: list[object],
) -> None:
    _stub_run(monkeypatch, _completed(_payload(packets=packets)))

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
    _installed_ffprobe(monkeypatch)

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(ffprobe_module.subprocess, "run", run)

    with pytest.raises(MediaProbeUnavailableError):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


def test_undecodable_subprocess_output_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed_ffprobe(monkeypatch)

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(ffprobe_module.subprocess, "run", run)

    with pytest.raises(MediaProbeUnavailableError, match="undecodable output"):
        FfprobeMediaProbe().probe(Path("movie.mkv"))


def test_real_ffprobe_reports_bounded_packet_evidence_when_available(tmp_path: Path) -> None:
    """Smoke-test the real option/output contract without making it a host requirement."""
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if ffprobe is None or ffmpeg is None:
        pytest.skip("ffprobe/ffmpeg binaries are not installed")

    video = tmp_path / "packet-evidence.avi"
    try:
        subprocess.run(  # noqa: S603 -- absolute executable resolved from the test host
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16:d=0.1",
                "-c:v",
                "mpeg4",
                "-y",
                str(video),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("installed ffmpeg cannot generate the smoke-test fixture")

    result = FfprobeMediaProbe().probe(video)

    assert result.container == "avi"
    assert result.video_codec == "mpeg4"


def test_real_ffprobe_rejects_padded_zero_packet_video_when_available(tmp_path: Path) -> None:
    """A declared video track plus padding must not satisfy packet evidence."""
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if ffprobe is None or ffmpeg is None:
        pytest.skip("ffprobe/ffmpeg binaries are not installed")

    video = tmp_path / "zero-packet.mp4"
    try:
        subprocess.run(  # noqa: S603 -- absolute executable resolved from the test host
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16:r=1",
                "-frames:v",
                "0",
                "-c:v",
                "mpeg4",
                "-movflags",
                "frag_keyframe+empty_moov",
                "-f",
                "mp4",
                "-y",
                str(video),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("installed ffmpeg cannot generate the zero-packet fixture")

    # Sparse trailing padding puts the file above the import sample floor while
    # leaving its declared video track packetless.
    with video.open("r+b") as handle:
        handle.truncate(60_000_000)

    with pytest.raises(MediaProbeError, match="no readable packet") as raised:
        FfprobeMediaProbe().probe(video)
    assert not isinstance(raised.value, MediaProbeUnavailableError)


def test_real_ffprobe_output_omits_unrequested_metadata_when_available(
    tmp_path: Path,
) -> None:
    """Selected JSON fields must not capture download-controlled tags."""
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if ffprobe is None or ffmpeg is None:
        pytest.skip("ffprobe/ffmpeg binaries are not installed")

    marker = "SECRET_METADATA_MARKER"
    video = tmp_path / "metadata.avi"
    try:
        subprocess.run(  # noqa: S603 -- absolute executable resolved from the test host
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16:d=0.1",
                "-c:v",
                "mpeg4",
                "-metadata",
                f"comment={marker}",
                "-y",
                str(video),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("installed ffmpeg cannot generate the metadata fixture")

    completed = subprocess.run(  # noqa: S603 -- absolute executable from the test host
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "V",
            "-read_intervals",
            "%+#32",
            "-show_entries",
            (
                "format=format_name:stream=index,codec_type,codec_name:"
                "stream_disposition=attached_pic:packet=stream_index"
            ),
            "-of",
            "json=compact=1",
            "--",
            str(video),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10.0,
        shell=False,
    )

    result = FfprobeMediaProbe().probe(video)

    assert result.video_codec == "mpeg4"
    assert marker not in completed.stdout
    assert completed.stderr is None


def test_probe_result_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _completed(_payload()))
    result = FfprobeMediaProbe().probe(Path("movie.mkv"))

    with pytest.raises(ValidationError):
        result.container = "avi"  # pyright: ignore[reportAttributeAccessIssue]


def test_adapter_satisfies_sync_media_probe_protocol() -> None:
    probe: Callable[[Path], object] = FfprobeMediaProbe().probe
    assert callable(probe)
    assert isinstance(FfprobeMediaProbe(), MediaProbePort)
