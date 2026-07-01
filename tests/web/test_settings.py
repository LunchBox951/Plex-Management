"""Settings — GET redacts secrets; PUT round-trips and stores secrets encrypted."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.web.deps import (
    KNOWN_SETTING_KEYS,
    SettingsStore,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_eviction_enabled,
    get_eviction_grace_days,
    get_eviction_interval_minutes,
    get_eviction_proactive_enabled,
    get_log_retention_days,
    get_movies_root_optional,
    get_tv_root_optional,
)
from plex_manager.web.schemas import SettingsResponse, SettingsUpdate

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "settings-key"


def test_every_known_setting_key_has_a_response_and_update_field() -> None:
    """Regression guard for the operability beta's original defect: every
    ``KNOWN_SETTING_KEYS`` entry (what ``SettingsStore.redacted()`` always
    returns a value for) must be a real field on BOTH ``SettingsResponse`` and
    ``SettingsUpdate`` -- otherwise it is readable/writable only via a direct
    DB edit, which violates the "100% web-operable" north star. The 7
    eviction/log-retention settings were once present in ``KNOWN_SETTING_KEYS``
    but absent from both schemas."""
    for key in KNOWN_SETTING_KEYS:
        assert key in SettingsResponse.model_fields, f"{key} missing from SettingsResponse"
        assert key in SettingsUpdate.model_fields, f"{key} missing from SettingsUpdate"


def test_settings_update_rejects_target_above_threshold() -> None:
    # R2-2: a disk_pressure_target above the trigger threshold makes every root in the
    # [threshold, target] band read "under pressure" yet select nothing -> a silent
    # dead band. When both are sent together it must be a visible 422, not accepted.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=90.0)
    # equal and below the threshold are both fine.
    SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=80.0)
    SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=70.0)


async def test_get_starts_empty(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    body = response.json()
    assert body["plex_url"] is None
    assert body["tmdb_api_key"] is None


async def test_get_starts_with_tv_root_unset(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.json()["tv_root"] is None


async def test_put_tv_root_round_trips_independently_of_movies_root(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # tv_root is a plain (non-secret) path, just like movies_root, and settable
    # without touching movies_root -- the two roots are independently optional.
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings", json={"tv_root": "/library/tv"}, headers={"X-Api-Key": _API_KEY}
    )
    assert put.status_code == 200
    assert put.json()["tv_root"] == "/library/tv"
    assert put.json()["movies_root"] is None

    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["tv_root"] == "/library/tv"


async def test_put_round_trips_and_redacts(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    update = {"plex_url": "http://plex.local:32400", "tmdb_api_key": "super-secret-key"}
    put = await client.put("/api/v1/settings", json=update, headers={"X-Api-Key": _API_KEY})
    assert put.status_code == 200
    put_body = put.json()
    assert put_body["plex_url"] == "http://plex.local:32400"
    assert put_body["tmdb_api_key"] == "***"
    assert "super-secret-key" not in put.text

    # GET reflects the same redacted view.
    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["plex_url"] == "http://plex.local:32400"
    assert got["tmdb_api_key"] == "***"


async def test_secret_is_stored_encrypted(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    plaintext = "another-secret-key"
    await client.put(
        "/api/v1/settings",
        json={"tmdb_api_key": plaintext, "plex_url": "http://plex.local"},
        headers={"X-Api-Key": _API_KEY},
    )

    # Inspect the raw columns, bypassing the EncryptedStr decryption layer.
    async with sessionmaker_() as session:
        secret_row = (
            await session.execute(
                text(
                    "SELECT value, encrypted_value, is_secret "
                    "FROM settings WHERE key = 'tmdb_api_key'"
                )
            )
        ).one()
        plain_row = (
            await session.execute(
                text("SELECT value, encrypted_value FROM settings WHERE key = 'plex_url'")
            )
        ).one()

    raw_value, raw_encrypted, is_secret = secret_row
    assert bool(is_secret) is True
    assert raw_value is None  # the plaintext column is never used for a secret
    assert raw_encrypted is not None
    assert plaintext not in raw_encrypted  # at-rest value is ciphertext, not plaintext

    # The non-secret url is stored in the plaintext column, unencrypted.
    assert plain_row[0] == "http://plex.local"
    assert plain_row[1] is None


async def test_put_mask_round_trip_does_not_clobber_secret(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    # Establish a real secret.
    await client.put(
        "/api/v1/settings",
        json={"tmdb_api_key": "real-tmdb-secret", "plex_url": "http://plex.local"},
        headers=headers,
    )

    # FE GETs the redacted view (secret shows as the mask), edits only a non-secret
    # field, and PUTs the whole object back verbatim — mask and all.
    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got["tmdb_api_key"] == "***"
    got["plex_url"] = "http://plex.local:32400"
    put = await client.put("/api/v1/settings", json=got, headers=headers)
    assert put.status_code == 200
    assert put.json()["plex_url"] == "http://plex.local:32400"

    # The real secret must survive — the mask write was a no-op, not a wipe.
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("tmdb_api_key") == "real-tmdb-secret"


async def test_empty_string_root_reads_back_as_unset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """PUT only skips a field when it's ``None`` (absent) — an empty-string
    ``movies_root``/``tv_root`` (e.g. a frontend "clear" that submits ``""``
    instead of omitting the field) is written verbatim. The importer's
    ``get_*_root_optional`` deps must still report that as unset (``None``), not
    a falsy-but-truthy-looking path: otherwise it would sail past a downstream
    ``is None`` guard and silently resolve relative paths against the process
    CWD instead of tripping the honest ``ImportBlocked`` it's meant to."""
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"movies_root": "", "tv_root": ""},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") == ""
        assert await SettingsStore(session).get("tv_root") == ""
        assert await get_movies_root_optional(session) is None
        assert await get_tv_root_optional(session) is None


# --------------------------------------------------------------------------- #
# Operability beta (ADR-0012) settings: disk-pressure eviction + log retention
# --------------------------------------------------------------------------- #
async def test_put_round_trips_operability_settings(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    update = {
        "disk_pressure_threshold_percent": 88.5,
        "disk_pressure_target_percent": 75,
        "eviction_grace_days": 14,
        "eviction_enabled": False,
        "eviction_proactive_enabled": True,
        "eviction_interval_minutes": 45,
        "log_retention_days": 3,
    }
    put = await client.put("/api/v1/settings", json=update, headers=headers)
    assert put.status_code == 200
    body = put.json()
    assert body["disk_pressure_threshold_percent"] == 88.5
    assert body["disk_pressure_target_percent"] == 75.0
    assert body["eviction_grace_days"] == 14
    assert body["eviction_enabled"] is False
    assert body["eviction_proactive_enabled"] is True
    assert body["eviction_interval_minutes"] == 45.0
    assert body["log_retention_days"] == 3

    # GET reflects the identical stored values.
    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got == body

    # The typed getters the eviction/log-retention loops actually read must see
    # the SAME values -- not just a wire-level round trip (guards against e.g.
    # a bool serialized in a form the case-insensitive parser wouldn't accept).
    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 88.5
        assert await get_disk_pressure_target_percent(session) == 75.0
        assert await get_eviction_grace_days(session) == 14
        assert await get_eviction_enabled(session) is False
        assert await get_eviction_proactive_enabled(session) is True
        assert await get_eviction_interval_minutes(session) == 45.0
        assert await get_log_retention_days(session) == 3


async def test_put_rejects_out_of_range_operability_settings(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    over_100 = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 150},
        headers=headers,
    )
    assert over_100.status_code == 422

    zero_interval = await client.put(
        "/api/v1/settings",
        json={"eviction_interval_minutes": 0},
        headers=headers,
    )
    assert zero_interval.status_code == 422

    negative_days = await client.put(
        "/api/v1/settings",
        json={"log_retention_days": -1},
        headers=headers,
    )
    assert negative_days.status_code == 422


async def test_put_single_field_threshold_below_stored_target_rejects_and_does_not_persist(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """R4-2: ``SettingsUpdate``'s own ``model_validator`` only catches a target
    above the threshold when BOTH fields are sent in the SAME request -- ``PUT``
    is a PARTIAL update, so a request naming just ONE side against an
    already-stored (now-inverted) other side must ALSO 422, cross-checked
    against what is actually persisted (see
    ``routers.settings._validate_disk_pressure_pair``), or the whole
    threshold-to-target band silently stops relieving pressure."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    # Establish a stored target of 80 (paired with a valid threshold of 95).
    seeded = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 95.0, "disk_pressure_target_percent": 80.0},
        headers=headers,
    )
    assert seeded.status_code == 200

    # A split update naming ONLY the threshold, now BELOW the stored target (80).
    put = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 70.0},
        headers=headers,
    )
    assert put.status_code == 422

    # Never persisted -- both sides stay at their last valid stored values.
    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 95.0
        assert await get_disk_pressure_target_percent(session) == 80.0


async def test_put_single_field_threshold_above_stored_target_still_succeeds(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The stored-value cross-check only rejects an INVERTED effective pair -- a
    valid partial update (threshold alone, still above the stored target) must
    succeed normally, never over-rejected by the new check."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    seeded = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 85.0, "disk_pressure_target_percent": 80.0},
        headers=headers,
    )
    assert seeded.status_code == 200

    put = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 90.0},
        headers=headers,
    )
    assert put.status_code == 200
    assert put.json()["disk_pressure_threshold_percent"] == 90.0

    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 90.0
        assert await get_disk_pressure_target_percent(session) == 80.0  # untouched
