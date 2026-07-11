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


def test_subscriber_added_during_dispatch_is_not_invoked_until_next_publish() -> None:
    """``publish`` must iterate a SNAPSHOT of the subscriber list (issue #110):
    a handler that subscribes a new handler while dispatch is in flight must not
    affect the CURRENT publish — the newly-added handler is only invoked
    starting from the NEXT publish, never the one that triggered its
    registration. Without the snapshot, mutating ``self._subscribers`` mid-
    iteration could skip or double-invoke a handler depending on exactly where
    the mutation lands relative to the live list."""
    bus = EventBus()
    calls: list[str] = []

    def late_handler(_event: DownloadFailed) -> None:
        calls.append("late")

    def self_registering_handler(_event: DownloadFailed) -> None:
        calls.append("first")
        bus.subscribe(DownloadFailed, late_handler)

    bus.subscribe(DownloadFailed, self_registering_handler)

    bus.publish(DownloadFailed(torrent_hash="abc", source_title="Bad.Release", reason="stalled"))
    assert calls == ["first"]  # late_handler registered too late for THIS publish

    bus.publish(DownloadFailed(torrent_hash="def", source_title="Bad.Release", reason="stalled"))
    assert calls == ["first", "first", "late"]  # now invoked on the NEXT publish


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
