"""QbittorrentClient — the live :class:`DownloadClientPort` impl (qBittorrent v2).

Talks to the qBittorrent WebUI API (``/api/v2``) over an injected
``httpx.AsyncClient``. Authentication is cookie-based: ``POST /auth/login`` yields
an ``SID`` cookie (held by the client's cookie jar); a 403 on any later call
triggers a transparent re-login. The username, password and ``SID`` are NEVER
logged.

``add`` returns the lowercased info-hash. It accepts a magnet URI directly, or an
HTTP(S) URL which is resolved: a redirect to a magnet is followed (qBittorrent
cannot follow HTTP->magnet itself), and a body that is a ``.torrent`` file is sent
as multipart with its SHA-1 info-hash computed locally. A 409 (already present)
is treated as success and resolves to the existing hash.

``get_status`` / ``get_all_statuses`` read ``/torrents/info`` and map each torrent
to the port's :class:`DownloadStatus` DTO, keeping the qBittorrent ``state`` string
verbatim in ``raw_state`` (the domain reconciler owns the raw->domain mapping).
``/torrents/properties`` is consulted (cached) for the authoritative save path.

``pause`` / ``resume`` adapt to the server version: qBittorrent 5.0 (WebAPI
2.11.0) renamed ``/torrents/pause`` -> ``/torrents/stop`` and ``/torrents/resume``
-> ``/torrents/start`` (the old paths 404 on a 5.x server). The WebAPI version is
read once from ``/app/webapiVersion`` (cached) and the correct endpoint chosen, so
the adapter works against both qBit 4.x and 5.x.

Salvaged from the prototype (read, re-typed for async + pyright strict): the
torrent-add success normalisation, the bencode SHA-1 info-hash extraction, the
HTTP->magnet redirect walker, and the 409-as-success behaviour.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import socket
from datetime import UTC, datetime
from typing import Final, cast
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from plex_manager.ports.download_client import DownloadedFile, DownloadStatus

__all__ = ["QbittorrentAuthError", "QbittorrentClient", "QbittorrentError"]

_logger = logging.getLogger(__name__)

_API: Final = "/api/v2"
_HTTP_OK: Final = 200
_HTTP_NO_CONTENT: Final = 204
_HTTP_FORBIDDEN: Final = 403
_HTTP_CONFLICT: Final = 409
_REDIRECT_MAX_DEPTH: Final = 5
_PROPERTIES_TTL_SECONDS: Final = 30.0
_MAX_TORRENT_BYTES: Final = 1_000_000
# WebAPI 2.11.0 (qBittorrent 5.0) renamed pause/resume to stop/start.
_STOP_START_MIN_WEBAPI: Final = (2, 11, 0)


class QbittorrentError(RuntimeError):
    """Base for surfaced qBittorrent failures (transport outage or HTTP error).

    Raised instead of letting httpx's transport / status errors escape: those
    propagate as an opaque 500 and embed the request url (and, on login, the
    credentials in the posted body never reaches the message, but the url could).
    Converting at the boundary keeps the failure visible and retryable (honesty
    over silence) without leaking any secret — the message carries the status code
    only, never the url, username, password or session id.
    """


class QbittorrentAuthError(QbittorrentError):
    """Raised when qBittorrent rejects the login (bad credentials / banned IP).

    A surfaced error — never a silent failure. The message never includes the
    password or session id.
    """


# --------------------------------------------------------------------------- #
# Pure helpers (bencode / magnet) — salvaged from the prototype, re-typed.
# --------------------------------------------------------------------------- #
def _bencode_skip(data: bytes, idx: int) -> int:
    """Return the index just past the bencode element starting at ``idx``.

    Walks the structure without materialising values — enough to find the raw
    byte boundaries of the ``info`` dict for hashing. Raises ``ValueError`` on
    malformed input.
    """
    if idx >= len(data):
        raise ValueError("unexpected end of bencode data")
    ch = data[idx : idx + 1]
    if ch == b"i":  # integer: i<number>e
        return data.index(b"e", idx + 1) + 1
    if ch in (b"l", b"d"):  # list / dict: container<elements>e
        idx += 1
        while data[idx : idx + 1] != b"e":
            idx = _bencode_skip(data, idx)
        return idx + 1
    if ch.isdigit():  # byte string: <length>:<bytes>
        colon = data.index(b":", idx)
        length = int(data[idx:colon])
        return colon + 1 + length
    raise ValueError(f"invalid bencode at position {idx}: {ch!r}")


def _info_hash_from_torrent(data: bytes) -> str | None:
    """SHA-1 of the raw bencoded ``info`` dict — the BitTorrent info-hash.

    Returns the lowercased hex digest, or ``None`` if the structure can't be
    located (never raises — the caller treats ``None`` as "couldn't derive").
    """
    marker = b"4:infod"
    idx = data.find(marker)
    if idx == -1:
        return None
    info_start = idx + len(marker) - 1  # position of the 'd' opening the dict
    try:
        info_end = _bencode_skip(data, info_start)
    except ValueError:
        return None
    return hashlib.sha1(data[info_start:info_end]).hexdigest().lower()  # noqa: S324


def _normalize_btih(value: str) -> str:
    """Normalise a ``btih`` info-hash to the 40-char lowercase hex qBittorrent uses.

    A magnet ``xt=urn:btih:`` value is either the 40-char hex form or the valid
    32-char base32 encoding of the same 20-byte hash. qBittorrent always reports
    the hex form, so a base32 magnet must be decoded to hex — otherwise the stored
    hash never matches the client snapshot and the reconciler treats the torrent as
    ``ClientMissing`` forever. A 40-char hex value is returned lowercased; any other
    shape is passed through lowercased (best effort — nothing is swallowed).
    """
    if len(value) == 32:
        try:
            # b32decode raises binascii.Error (a ValueError subclass) on bad input.
            return base64.b32decode(value.upper()).hex()
        except ValueError:
            return value.lower()
    return value.lower()


def _info_hash_from_magnet(magnet: str) -> str | None:
    """Extract the info-hash from a magnet URI's ``xt=urn:btih:`` parameter."""
    parsed = urlparse(magnet)
    if parsed.scheme != "magnet":
        return None
    for xt in parse_qs(parsed.query).get("xt", []):
        if xt.startswith("urn:btih:"):
            return _normalize_btih(xt[len("urn:btih:") :])
    return None


def _is_add_success(response: httpx.Response) -> bool:
    """Normalise qBittorrent's varied ``/torrents/add`` success signals.

    ``Ok.`` text, a 409 (already present), or a JSON body reporting added/pending
    ids all count as success. Salvaged from the prototype's
    ``_is_torrent_add_success``.
    """
    if response.status_code == _HTTP_CONFLICT:
        return True
    if response.status_code not in (_HTTP_OK, _HTTP_NO_CONTENT):
        return False
    text = response.text.strip()
    if text in ("Ok.", ""):
        return True
    if text.startswith("{"):
        try:
            data = _as_dict(response.json())
        except ValueError:
            return False
        if _i(data.get("success_count")) > 0:
            return True
        if _i(data.get("pending_count")) > 0:
            return True
        ids = data.get("added_torrent_ids")
        if isinstance(ids, list) and ids:
            return True
    return False


def _decode_json(response: httpx.Response, what: str) -> object:
    """Decode a response body as JSON, converting a non-JSON body to a typed error.

    A 200 whose body is not JSON (a reverse-proxy / auth HTML page in front of the
    WebUI) would otherwise raise a raw ``JSONDecodeError`` that bypasses the
    ``QbittorrentError`` handler and surfaces as an opaque 500. Converting it here
    keeps the failure visible and retryable; the message names only the endpoint,
    never the url or any secret.
    """
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise QbittorrentError(f"qBittorrent returned a non-JSON body for {what}") from exc


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an untyped JSON node to a string-keyed dict (else empty)."""
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return {}


def _as_list(value: object) -> list[object]:
    """Narrow an untyped JSON node to a list (else empty)."""
    if isinstance(value, list):
        return cast("list[object]", value)
    return []


def _f(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _i(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _s(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _parse_webapi_version(text: str) -> tuple[int, ...]:
    """Parse a qBittorrent WebAPI version string (e.g. ``"2.11.0"``) into a tuple.

    Stops at the first non-numeric component; returns ``()`` if none parse.
    """
    parts: list[int] = []
    for chunk in text.strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _is_blocked_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _assert_safe_fetch_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise QbittorrentError("unsupported torrent source URL")
    host = parsed.hostname
    if _is_blocked_address(host):
        raise QbittorrentError("unsafe torrent source URL")
    try:
        infos = socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
    except OSError:
        # The request itself will surface an honest reachability error. This keeps
        # mocked/unresolvable test hostnames usable while still rejecting resolvable
        # private targets.
        return
    if any(isinstance(info[4][0], str) and _is_blocked_address(info[4][0]) for info in infos):
        raise QbittorrentError("unsafe torrent source URL")


def _torrent_to_status(torrent: dict[str, object]) -> DownloadStatus:
    """Map one ``/torrents/info`` row to the port's ``DownloadStatus`` DTO.

    ``raw_state`` is kept verbatim; ``content_path`` is dropped when it merely
    echoes ``save_path`` (qBittorrent does that for not-yet-resolved torrents).
    """
    save_path = _s(torrent.get("save_path"))
    content_path = _s(torrent.get("content_path")) or None
    if content_path is not None and content_path == save_path:
        content_path = None
    eta = _i(torrent.get("eta"))
    return DownloadStatus(
        info_hash=_s(torrent.get("hash")).lower(),
        name=_s(torrent.get("name")),
        raw_state=_s(torrent.get("state")),
        progress=_f(torrent.get("progress")),
        ratio=_f(torrent.get("ratio")),
        save_path=save_path,
        content_path=content_path,
        eta_seconds=eta if eta > 0 else None,
        ratio_limit=_f(torrent.get("ratio_limit"), -2.0),
        seeding_time_limit_minutes=_i(torrent.get("seeding_time_limit"), -2),
        inactive_seeding_time_limit_minutes=_i(torrent.get("inactive_seeding_time_limit"), -2),
        last_activity_unix=_i(torrent.get("last_activity")),
    )


class QbittorrentClient:
    """Add, monitor and control torrents. Implements ``DownloadClientPort``."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        username: str,
        password: str,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._logged_in = False
        # info_hash -> (fetched_at, properties json) — bounds /properties calls.
        self._properties_cache: dict[str, tuple[datetime, dict[str, object]]] = {}
        # Cached pause/resume-vs-stop/start decision (None until first probed).
        self._stop_start: bool | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial, redacts secrets
        return (
            f"QbittorrentClient(base_url={self._base_url!r}, "
            f"username=<redacted>, password=<redacted>)"
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Convert a non-2xx response into a wrapped :class:`QbittorrentError`.

        Replaces ``httpx.Response.raise_for_status``: its ``HTTPStatusError`` would
        propagate as an opaque 500 and embeds the request url. The wrapped error
        carries the status code only — never the url or any secret. Auth (403) is
        already handled by ``_request``'s transparent re-login, so a status reaching
        here is a genuine, surfaced failure.
        """
        if response.is_error:
            raise QbittorrentError(f"qBittorrent request failed (HTTP {response.status_code})")

    # ---- auth ----------------------------------------------------------- #
    async def _login(self) -> None:
        """Authenticate and capture the ``SID`` cookie in the client jar."""
        try:
            response = await self._client.post(
                f"{self._base_url}{_API}/auth/login",
                data={"username": self._username, "password": self._password},
                headers={"Referer": self._base_url},
            )
        except httpx.RequestError as exc:
            # qBittorrent unreachable during login (DNS / refused / timeout): surface
            # a retryable error rather than an opaque 500. No url/secret in the message.
            raise QbittorrentError("qBittorrent request failed") from exc
        text = response.text.strip()
        status = response.status_code
        if status in (_HTTP_OK, _HTTP_NO_CONTENT) and text != "Fails.":
            self._logged_in = True
            _logger.info("authenticated with qBittorrent")
            return
        # Genuine auth rejection: a 200 "Fails." body (bad credentials) or a 403
        # (IP banned after repeated failures). Only these route the operator to the
        # credential-reset correction path.
        if status == _HTTP_FORBIDDEN or (
            status in (_HTTP_OK, _HTTP_NO_CONTENT) and text == "Fails."
        ):
            raise QbittorrentAuthError(
                f"qBittorrent rejected the login (HTTP {status}): check the username and password"
            )
        # Any other non-2xx (5xx / 404 — the WebUI or a reverse proxy in front of
        # it is down) is a retryable OUTAGE, not an auth failure: surface it as a
        # QbittorrentError so the operator isn't wrongly told to reset credentials.
        raise QbittorrentError(f"qBittorrent login failed (HTTP {status})")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """Send an authenticated request, re-logging in once on a 403."""
        if not self._logged_in:
            await self._login()
        url = f"{self._base_url}{_API}{path}"
        try:
            response = await self._client.request(
                method, url, params=params, data=data, files=files
            )
            if response.status_code == _HTTP_FORBIDDEN:
                self._logged_in = False
                await self._login()
                response = await self._client.request(
                    method, url, params=params, data=data, files=files
                )
        except httpx.RequestError as exc:
            # qBittorrent down or network failure mid-request: surface a retryable
            # error rather than letting httpx's transport error escape as an opaque
            # 500. The message carries no url or secret.
            raise QbittorrentError("qBittorrent request failed") from exc
        return response

    # ---- add ------------------------------------------------------------ #
    async def _resolve_http_source(self, url: str) -> tuple[str | None, bytes | None]:
        """Walk an HTTP(S) source to a magnet URI or ``.torrent`` body.

        Returns ``(magnet_uri, None)`` if a redirect leads to a magnet, or
        ``(None, torrent_bytes)`` if the URL serves a bencoded ``.torrent``.
        qBittorrent cannot follow HTTP->magnet redirects itself, hence this walk.
        """
        current = url
        for _ in range(_REDIRECT_MAX_DEPTH):
            _assert_safe_fetch_url(current)
            try:
                async with self._client.stream("GET", current, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location", "")
                        if not location:
                            break
                        if location.startswith("magnet:"):
                            return location, None
                        current = urljoin(current, location)
                        continue
                    if response.status_code == _HTTP_OK:
                        content_length = response.headers.get("Content-Length")
                        if content_length is not None:
                            try:
                                if int(content_length) > _MAX_TORRENT_BYTES:
                                    raise QbittorrentError("torrent file is too large")
                            except ValueError:
                                pass
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > _MAX_TORRENT_BYTES:
                                raise QbittorrentError("torrent file is too large")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        if body[:1] == b"d":
                            return None, body
                    break
            except httpx.RequestError as exc:
                # Indexer/Prowlarr download_url unreachable (DNS / refused / timeout):
                # surface a retryable error rather than letting httpx's transport error
                # escape as an opaque 500 on the grab path. No url/secret in the message.
                raise QbittorrentError("qBittorrent request failed") from exc
        return None, None

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> str:
        """Add a torrent; return its lowercased info-hash.

        A 409 (already present) resolves to the computed hash rather than erroring.
        """
        urls_value: str | None = None
        torrent_bytes: bytes | None = None
        info_hash: str | None = None

        if magnet_or_url.startswith("magnet:"):
            urls_value = magnet_or_url
            info_hash = _info_hash_from_magnet(magnet_or_url)
        elif magnet_or_url.startswith("http"):
            magnet, body = await self._resolve_http_source(magnet_or_url)
            if magnet is not None:
                urls_value = magnet
                info_hash = _info_hash_from_magnet(magnet)
            elif body is not None:
                torrent_bytes = body
                info_hash = _info_hash_from_torrent(body)
            else:
                # Could not resolve to a magnet or locally hashable .torrent. Do
                # not ask qBittorrent to add an untrackable opaque URL.
                raise QbittorrentError("could not determine torrent hash for HTTP source")
        else:
            urls_value = magnet_or_url
            info_hash = _info_hash_from_magnet(magnet_or_url)

        form: dict[str, str] = {"savepath": save_path, "category": category}
        files: dict[str, tuple[str, bytes, str]] | None = None
        if urls_value is not None:
            form["urls"] = urls_value
        if torrent_bytes is not None:
            files = {"torrents": ("file.torrent", torrent_bytes, "application/x-bittorrent")}

        response = await self._request("POST", "/torrents/add", data=form, files=files)
        if not _is_add_success(response):
            # Surfaced, retryable failure — never an opaque 500. No url/secret leak.
            raise QbittorrentError(
                f"qBittorrent rejected the torrent (HTTP {response.status_code})"
            )

        if info_hash is not None:
            return info_hash.lower()
        # No locally-derivable hash (rare: opaque .torrent URL qBit fetched). Best
        # effort: the caller can reconcile by category on the next poll.
        _logger.warning("added torrent but could not derive its info-hash locally")
        return ""

    # ---- status --------------------------------------------------------- #
    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        """Return the status for ``info_hash``, or ``None`` if absent."""
        response = await self._request(
            "GET", "/torrents/info", params={"hashes": info_hash.lower()}
        )
        self._raise_for_status(response)
        rows = _as_list(_decode_json(response, "/torrents/info"))
        if rows:
            return _torrent_to_status(_as_dict(rows[0]))
        return None

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        """Return statuses for all torrents, optionally filtered by category."""
        params: dict[str, str] = {}
        if category is not None:
            params["category"] = category
        response = await self._request("GET", "/torrents/info", params=params)
        self._raise_for_status(response)
        out: list[DownloadStatus] = []
        for row in _as_list(_decode_json(response, "/torrents/info")):
            mapped = _as_dict(row)
            if mapped:
                out.append(_torrent_to_status(mapped))
        return out

    # ---- control -------------------------------------------------------- #
    async def _use_stop_start(self) -> bool:
        """Whether to use the qBit-5 ``stop``/``start`` endpoints (cached probe).

        Reads ``/app/webapiVersion`` once; WebAPI >= 2.11.0 (qBit 5.0) renamed
        pause/resume to stop/start. If the version cannot be read, default to the
        modern endpoints — the version this adapter primarily targets.
        """
        if self._stop_start is None:
            response = await self._request("GET", "/app/webapiVersion")
            if response.status_code == _HTTP_OK:
                self._stop_start = _parse_webapi_version(response.text) >= _STOP_START_MIN_WEBAPI
            else:
                self._stop_start = True
        return self._stop_start

    async def pause(self, info_hash: str) -> None:
        """Pause (qBit 4.x) / stop (qBit 5.x) the torrent ``info_hash``."""
        path = "/torrents/stop" if await self._use_stop_start() else "/torrents/pause"
        response = await self._request("POST", path, data={"hashes": info_hash.lower()})
        self._raise_for_status(response)

    async def resume(self, info_hash: str) -> None:
        """Resume (qBit 4.x) / start (qBit 5.x) the torrent ``info_hash``."""
        path = "/torrents/start" if await self._use_stop_start() else "/torrents/resume"
        response = await self._request("POST", path, data={"hashes": info_hash.lower()})
        self._raise_for_status(response)

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        """Remove the torrent, deleting its files when ``delete_files`` is set."""
        response = await self._request(
            "POST",
            "/torrents/delete",
            data={
                "hashes": info_hash.lower(),
                "deleteFiles": "true" if delete_files else "false",
            },
        )
        self._raise_for_status(response)
        self._properties_cache.pop(info_hash.lower(), None)

    async def set_category(self, info_hash: str, category: str) -> None:
        """Set the torrent's category (used to mark imported items)."""
        response = await self._request(
            "POST",
            "/torrents/setCategory",
            data={"hashes": info_hash.lower(), "category": category},
        )
        self._raise_for_status(response)

    async def get_save_path(self, info_hash: str) -> str | None:
        """Return the torrent's current save path, re-read from the client."""
        properties = await self._fetch_properties(info_hash.lower())
        if properties is None:
            return None
        save_path = _s(properties.get("save_path"))
        return save_path or None

    async def list_files(self, info_hash: str) -> list[DownloadedFile]:
        """Return the torrent's files (relative path + size) for the importer.

        Reads ``/torrents/files``; maps each entry's ``name`` (path relative to the
        save path) and ``size`` (bytes). An empty/normal no-files response yields
        ``[]``; transport / auth failures surface the typed error (never swallowed).
        """
        response = await self._request("GET", "/torrents/files", params={"hash": info_hash.lower()})
        self._raise_for_status(response)
        out: list[DownloadedFile] = []
        for row in _as_list(_decode_json(response, "/torrents/files")):
            entry = _as_dict(row)
            if entry:
                out.append(
                    DownloadedFile(name=_s(entry.get("name")), size_bytes=_i(entry.get("size")))
                )
        return out

    async def _fetch_properties(self, info_hash: str) -> dict[str, object] | None:
        """Fetch ``/torrents/properties`` for ``info_hash``, cached briefly.

        Bounds the call rate: a cached value younger than the TTL is reused so a
        reconciler cycle does not hammer the endpoint.
        """
        now = datetime.now(UTC)
        cached = self._properties_cache.get(info_hash)
        if cached is not None:
            fetched_at, value = cached
            if (now - fetched_at).total_seconds() < _PROPERTIES_TTL_SECONDS:
                return value
        response = await self._request("GET", "/torrents/properties", params={"hash": info_hash})
        if response.status_code != _HTTP_OK:
            return None
        payload = _as_dict(_decode_json(response, "/torrents/properties"))
        if not payload:
            return None
        self._properties_cache[info_hash] = (now, payload)
        return payload
