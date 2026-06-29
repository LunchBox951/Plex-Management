"""Tests for the in-process event bus."""

from __future__ import annotations

from dataclasses import dataclass

from plex_manager.domain.events import DownloadFailed, Event, EventBus


def test_publish_invokes_subscribers_in_registration_order() -> None:
    bus = EventBus()
    order: list[str] = []
    bus.subscribe(DownloadFailed, lambda _event: order.append("blocklist"))
    bus.subscribe(DownloadFailed, lambda _event: order.append("research"))

    bus.publish(DownloadFailed(torrent_hash="abc", source_title="Bad.Release", reason="stalled"))

    assert order == ["blocklist", "research"]


def test_handler_receives_the_event_payload() -> None:
    bus = EventBus()
    seen: list[DownloadFailed] = []
    bus.subscribe(DownloadFailed, seen.append)

    event = DownloadFailed(
        torrent_hash="DEADBEEF",
        source_title="Movie.2024.CAM-GRP",
        reason="failed",
        tmdb_id=42,
        indexer="Idx",
    )
    bus.publish(event)

    assert seen == [event]
    assert seen[0].tmdb_id == 42


def test_publish_with_no_subscribers_is_a_noop() -> None:
    bus = EventBus()
    bus.publish(DownloadFailed(torrent_hash="x", source_title="y", reason="z"))


def test_subscribers_are_keyed_by_concrete_event_type() -> None:
    @dataclass(frozen=True)
    class OtherEvent(Event):
        note: str

    bus = EventBus()
    calls: list[str] = []
    bus.subscribe(DownloadFailed, lambda _event: calls.append("download_failed"))
    bus.subscribe(OtherEvent, lambda _event: calls.append("other"))

    bus.publish(OtherEvent(note="hi"))

    assert calls == ["other"]
