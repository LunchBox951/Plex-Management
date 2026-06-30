"""PlexLibrary stub tests — honest NotImplementedError, no token leak."""

from __future__ import annotations

import re

import httpx
import pytest

from plex_manager.adapters.plex import PlexLibrary


def _stub() -> PlexLibrary:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    return PlexLibrary(client, base_url="http://plex:32400", token="super-secret-token")  # noqa: S106


async def test_is_available_raises_clear_deferral() -> None:
    with pytest.raises(
        NotImplementedError, match=re.escape("deferred to v1: PlexLibrary.is_available")
    ):
        await _stub().is_available(123, "movie")


async def test_trigger_scan_raises_clear_deferral() -> None:
    with pytest.raises(
        NotImplementedError, match=re.escape("deferred to v1: PlexLibrary.trigger_scan")
    ):
        await _stub().trigger_scan("/data/movies")


async def test_list_sections_raises_clear_deferral() -> None:
    with pytest.raises(
        NotImplementedError, match=re.escape("deferred to v1: PlexLibrary.list_sections")
    ):
        await _stub().list_sections()


def test_repr_redacts_token() -> None:
    rendered = repr(_stub())
    assert "super-secret-token" not in rendered
    assert "***" in rendered


def test_stub_satisfies_library_port() -> None:
    from plex_manager.ports.library import LibraryPort

    assert isinstance(_stub(), LibraryPort)
