"""QbittorrentClient — the live :class:`DownloadClientPort` impl (qBittorrent v2).

Talks to the qBittorrent WebUI API (``/api/v2``) over an injected
``httpx.AsyncClient``. Authentication is cookie-based: ``POST /auth/login`` yields
the legacy ``SID`` cookie or qBittorrent 5.2's ``QBT_SID_<port>`` cookie. Its exact
validated name/value pair is held by this adapter and sent explicitly only to its
configured service; a 403 on any later call triggers a transparent re-login. The
process-wide client cookie jar is never the authority because cookies do not include
ports and could otherwise cross configured services. The username, password and
session cookie are NEVER logged.

``add`` returns the lowercased info-hash. It accepts a magnet URI directly, or an
HTTP(S) URL which is resolved: a redirect to a magnet is followed (qBittorrent
cannot follow HTTP->magnet itself), and a body that is a ``.torrent`` file is sent
as multipart with its SHA-1 info-hash computed locally. A 409 (already present)
is treated as success and resolves to the existing hash.

``get_status`` / ``get_all_statuses`` / ``get_statuses_for_hashes`` all read
``/torrents/info`` and map each torrent to the port's :class:`DownloadStatus` DTO,
keeping the qBittorrent ``state`` string verbatim in ``raw_state`` (the domain
reconciler owns the raw->domain mapping). ``get_statuses_for_hashes`` is the
SCOPED variant (issue #216): it filters via the API's pipe-separated ``hashes``
param, chunked to a bounded URL size, so the frequent reconcile poll costs
proportional to Plex Manager's own tracked downloads rather than the shared
client's whole inventory; ``get_all_statuses`` remains for the few callers that
genuinely want the full inventory (e.g. setup validation).
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
import os
import re
import socket
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Sequence,
)
from datetime import UTC, datetime
from typing import Final, cast
from urllib.parse import ParseResult, parse_qs, urljoin, urlparse

import anyio.to_thread
import httpcore
import httpx

from plex_manager.adapters.service_url import InvalidServiceUrl, ServiceUrl
from plex_manager.ports.download_client import AddResult, DownloadedFile, DownloadStatus

__all__ = [
    "QbittorrentAuthError",
    "QbittorrentClient",
    "QbittorrentError",
    "QbittorrentSourceError",
]

_logger = logging.getLogger(__name__)

_API: Final = "/api/v2"
_HTTP_OK: Final = 200
_HTTP_NO_CONTENT: Final = 204
_HTTP_MULTIPLE_CHOICES: Final = 300
_HTTP_FORBIDDEN: Final = 403
_HTTP_CONFLICT: Final = 409
_REDIRECT_MAX_DEPTH: Final = 5
_PROPERTIES_TTL_SECONDS: Final = 30.0
# A .torrent metafile is normally tens of KB, but a large multi-file pack with a
# small piece size legitimately reaches a few MB. 10 MiB is comfortably above any
# real metafile yet still a hard ceiling against a hostile/unbounded source body.
_MAX_TORRENT_BYTES: Final = 10 * 1024 * 1024
# /torrents/info?hashes=a|b|c has no documented server-side limit, but an
# unbounded query string risks tripping a reverse proxy's URL/header length cap
# (nginx's default is 8 KiB). 100 lowercase 40-char hex hashes joined by "|" is
# ~4.1 KiB of query string -- comfortably under that even in a large household
# queue -- so get_statuses_for_hashes chunks larger tracked sets instead of
# growing the URL past this (issue #216).
_HASHES_PER_REQUEST: Final = 100
_QBT_SESSION_COOKIE_PREFIX: Final = "QBT_SID_"
_SESSION_COOKIE_NAME_PATTERN: Final = rf"(?:SID|{_QBT_SESSION_COOKIE_PREFIX}[1-9][0-9]{{0,4}})"
# qBittorrent's current SID is standard Base64; the unreserved additions retain
# compatibility with legacy values while excluding every Cookie-header delimiter.
_SESSION_COOKIE_VALUE_PATTERN: Final = r"[A-Za-z0-9+/._~-]+={0,2}"
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
# WebAPI 2.11.0 (qBittorrent 5.0) renamed pause/resume to stop/start.
_STOP_START_MIN_WEBAPI: Final = (2, 11, 0)
_HTTP_CORE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.ProtocolError,
    httpcore.ProxyError,
    httpcore.UnsupportedProtocol,
)


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


class QbittorrentSourceError(QbittorrentError):
    """A torrent SOURCE was vetoed or could not be resolved to a trackable torrent.

    Distinct from a client outage: qBittorrent itself is healthy and (in every
    case) was never asked to add anything — the SOURCE (an indexer download URL)
    is the problem. Every pre-client source veto raises this subtype, not the
    base class: an unsupported/malformed URL, an unsafe (non-global / NAT64 /
    unresolvable) fetch target, an oversized ``.torrent`` body (declared or
    streamed past the cap), a source fetch that failed in transport, or a body
    that is neither a magnet redirect nor a locally-hashable ``.torrent``.

    This taxonomy is load-bearing: the web layer maps it to a 422 (the request
    is well-formed but unprocessable) instead of the dishonest 502
    ``qbittorrent_unavailable`` that would blame a healthy client, and the
    auto-grab worker treats it as a PER-RELEASE failure (try the next accepted
    release, park on exhaustion) instead of aborting the whole cycle as a
    client outage.
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
        end = data.index(b"e", idx + 1)
        digits = data[idx + 1 : end]
        if digits[:1] == b"-":
            digits = digits[1:]
        if not digits.isdigit():
            # Non-digit garbage between i..e: honoring this docstring's "raises
            # on malformed input" contract keeps a mangled body from being
            # hashed as if it were a well-formed .torrent.
            raise ValueError(f"invalid bencode integer at position {idx}")
        return end + 1
    if ch in (b"l", b"d"):  # list / dict: container<elements>e
        idx += 1
        while idx < len(data) and data[idx : idx + 1] != b"e":
            idx = _bencode_skip(data, idx)
        if idx >= len(data):
            raise ValueError("unterminated bencode container")
        return idx + 1
    if ch.isdigit():  # byte string: <length>:<bytes>
        colon = data.index(b":", idx)
        length = int(data[idx:colon])
        end = colon + 1 + length
        if end > len(data):
            raise ValueError("bencode string extends past end of data")
        return end
    raise ValueError(f"invalid bencode at position {idx}: {ch!r}")


def _bencode_string(data: bytes, idx: int) -> tuple[bytes, int]:
    if idx >= len(data) or not data[idx : idx + 1].isdigit():
        raise ValueError(f"expected bencode string at position {idx}")
    colon = data.index(b":", idx)
    length = int(data[idx:colon])
    start = colon + 1
    end = start + length
    if end > len(data):
        raise ValueError("bencode string extends past end of data")
    return data[start:end], end


def _info_hash_from_torrent(data: bytes) -> str | None:
    """SHA-1 of the raw bencoded ``info`` dict — the BitTorrent info-hash.

    Returns the lowercased hex digest, or ``None`` if the structure can't be
    located (never raises — the caller treats ``None`` as "couldn't derive").
    """
    try:
        if data[:1] != b"d":
            return None
        idx = 1
        while True:
            if idx >= len(data):
                return None
            if data[idx : idx + 1] == b"e":
                return None
            key, value_start = _bencode_string(data, idx)
            value_end = _bencode_skip(data, value_start)
            if key == b"info":
                # The info VALUE must itself be a bencoded dict. Hashing an
                # arbitrary non-dict value (integer/string/list) would fabricate
                # an "info-hash" for a body that is not a torrent, letting an
                # invalid source reach /torrents/add — surfacing as a client
                # failure, or worse being tracked under a hash qBittorrent will
                # never report. No dict -> no hash derivable -> the caller's
                # no-hash path raises the per-release SourceError.
                if data[value_start : value_start + 1] != b"d":
                    return None
                return hashlib.sha1(data[value_start:value_end]).hexdigest().lower()  # noqa: S324
            idx = value_end
    except (ValueError, RecursionError):
        return None


def _normalize_btih(value: str) -> str:
    """Normalise a ``btih`` info-hash to the 40-char lowercase hex qBittorrent uses.

    A magnet ``xt=urn:btih:`` value is either the 40-char hex form or the valid
    32-char base32 encoding of the same 20-byte hash. qBittorrent always reports
    the hex form, so a base32 magnet must be decoded to hex — otherwise the stored
    hash never matches the client snapshot and the reconciler treats the torrent as
    ``ClientMissing`` forever. A valid 40-char hex value is returned lowercased; a
    valid 32-char base32 value is decoded to its hex form.

    A 32-char value that is NOT valid base32 is a MALFORMED source, not a hash:
    raising :class:`QbittorrentSourceError` routes it through the existing typed
    source-error taxonomy (the manual grab's ``torrent_source_unresolvable`` /
    auto-grab's per-release source-failure path) rather than passing the garbage
    through as if it were a real hash — a value that could never match the client
    snapshot, stranding the download as ``ClientMissing`` forever (north star #3:
    surface the malformed source, don't swallow it).
    """
    if len(value) == 32:
        try:
            # b32decode raises binascii.Error (a ValueError subclass) on
            # non-alphabet input, but a non-ASCII value raises a PLAIN
            # ValueError before the alphabet is even checked — catch the
            # base class so both malformed shapes stay in the taxonomy.
            decoded = base64.b32decode(value.upper())
        except ValueError as exc:
            raise QbittorrentSourceError("invalid base32 btih in magnet source") from exc
        # Padding tricks ("A"*31 + "=") decode "successfully" to fewer than the
        # 20 bytes a real info-hash has; the resulting short hex could never
        # match qBittorrent's 40-char snapshot, so it is the same malformed
        # source, not a hash.
        if len(decoded) != 20:
            raise QbittorrentSourceError("base32 btih decodes to a non-20-byte hash")
        return decoded.hex()
    return value.lower()


def _info_hash_from_magnet(magnet: str) -> str | None:
    """Extract the info-hash from a magnet URI's ``xt=urn:btih:`` parameter.

    Total: an UNPARSABLE magnet (an attacker-controlled redirect can supply e.g.
    ``magnet://[::1x/…`` whose bogus netloc makes ``urlparse`` raise ValueError)
    returns ``None`` — no hash derivable — instead of escaping the source-error
    taxonomy with a raw ValueError. A present-but-MALFORMED ``btih`` (invalid
    base32) instead raises :class:`QbittorrentSourceError` via
    :func:`_normalize_btih` — a typed source error IN the taxonomy, never garbage
    passed through as a hash (see that function's docstring).
    """
    try:
        parsed = urlparse(magnet)
    except ValueError:
        return None
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


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        embedded_v4: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded_v4.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded_v4.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded_v4.extend(ip.teredo)
        if ip in _NAT64_WELL_KNOWN_PREFIX:
            embedded_v4.append(ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF))
        if any(_is_blocked_ip(embedded) for embedded in embedded_v4):
            return True
    return False


def _is_blocked_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return _is_blocked_ip(ip)


def _safe_fetch_port(parsed: ParseResult, scheme: str) -> int:
    try:
        port = parsed.port
    except ValueError as exc:
        # A source-URL veto (qBittorrent never contacted) -> the SourceError
        # subtype, so the 422 mapping / per-release auto-grab handling apply.
        raise QbittorrentSourceError("unsupported torrent source URL") from exc
    return port or (443 if scheme == "https" else 80)


def _safe_fetch_addresses(host: str, port: int | None, allow_blocked: bool = False) -> list[str]:
    # All three vetoes below are SOURCE problems (an unsafe or unresolvable fetch
    # target; qBittorrent never contacted) -> the SourceError subtype, never the
    # base class that would read as a client outage. ``allow_blocked`` skips ONLY
    # the private/non-global address vetoes (never the resolve-failure one) for the
    # single operator-configured trusted source origin — the Prowlarr endpoint the
    # app already talks to for every API call (see ``_source_origin_triple``).
    if not allow_blocked and _is_blocked_address(host):
        raise QbittorrentSourceError("unsafe torrent source URL")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise QbittorrentSourceError("unsafe torrent source URL") from exc

    addresses = [info[4][0] for info in infos if isinstance(info[4][0], str)]
    if not addresses or (
        not allow_blocked and any(_is_blocked_address(address) for address in addresses)
    ):
        raise QbittorrentSourceError("unsafe torrent source URL")
    return addresses


async def _safe_fetch_addresses_async(
    host: str, port: int | None, allow_blocked: bool = False
) -> list[str]:
    """Resolve + vet ``host`` off the event loop.

    ``_safe_fetch_addresses`` calls the blocking ``socket.getaddrinfo``; run inside
    an async path (this and ``SafeFetchNetworkBackend.connect_tcp``) it would stall
    the whole event loop for the resolver's timeout. Offload to a worker thread so a
    slow or hostile DNS answer never blocks unrelated requests (httpx already pulls
    in anyio).
    """
    return await anyio.to_thread.run_sync(_safe_fetch_addresses, host, port, allow_blocked)


def _source_origin_triple(url: str) -> tuple[str, str, int] | None:
    """Normalize a URL to its ``(scheme, host, port)`` origin, or ``None``.

    The identity used to match a torrent-source hop against the OPERATOR-
    CONFIGURED Prowlarr endpoint: compared by URL origin (scheme + casefolded
    host + effective port), deliberately NEVER by resolved addresses — a
    hostile DNS answer must not be able to claim the trust. ``None`` for a
    non-http(s), hostless, malformed-port, or entirely UNPARSABLE URL (e.g. a
    bad IPv6 literal makes ``urlparse``/``parsed.hostname`` raise ValueError):
    nothing trustable — the caller keeps the SSRF veto fully closed rather
    than crashing outside the source-error taxonomy.
    """
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.casefold()
        if scheme not in ("http", "https") or not parsed.hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    return scheme, parsed.hostname.casefold(), port or (443 if scheme == "https" else 80)


async def _assert_safe_fetch_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError as exc:
        # An UNPARSABLE source URL (e.g. `http://[::1` — urlparse raises on the
        # bad IPv6 literal) is a release problem like any other malformed source:
        # SourceError, never a raw ValueError that 500s the manual grab and
        # aborts the auto-grab cycle.
        raise QbittorrentSourceError("unsupported torrent source URL") from exc
    if parsed.scheme not in ("http", "https") or not hostname:
        # A source-URL veto (e.g. a redirect hop left http(s)) -- SourceError, so
        # a hostile/broken redirect chain is a 422 release problem, not a "client
        # down" 502.
        raise QbittorrentSourceError("unsupported torrent source URL")
    port = _safe_fetch_port(parsed, parsed.scheme)
    await _safe_fetch_addresses_async(hostname, port)


class SafeFetchNetworkBackend(httpcore.AsyncNetworkBackend):
    """Pin HTTP-source fetches to a vetted global address.

    httpx/httpcore normally resolve the request hostname inside ``connect_tcp``.
    The adapter also validates torrent-source URLs before fetching, but that
    validation and the actual connection would otherwise be two DNS lookups. This
    backend makes the connection-time lookup the authority: it resolves once,
    rejects any non-global answer, then asks the real backend to connect to the
    vetted IP while httpcore keeps the original origin for Host and TLS SNI.

    ``trusted_host_port`` is the single OPERATOR-CONFIGURED trusted source
    endpoint (the Prowlarr host + effective port, derived from the same
    configured base URL the app already calls for every Prowlarr API request):
    a connection to exactly that host+port may resolve to a private address
    (``http://prowlarr:9696`` on a compose network, ``127.0.0.1``, RFC1918 —
    the normal self-hosted layout). Every other host keeps the full veto, so a
    redirect OFF the configured endpoint re-enters the normal SSRF guard.
    """

    def __init__(
        self,
        delegate: httpcore.AsyncNetworkBackend | None = None,
        trusted_host_port: tuple[str, int] | None = None,
    ) -> None:
        self._delegate: httpcore.AsyncNetworkBackend = (
            delegate
            if delegate is not None
            else cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())
        )
        self._trusted_host_port = trusted_host_port

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        trusted = self._trusted_host_port is not None and (
            (host.casefold(), port) == self._trusted_host_port
        )
        try:
            addresses = await _safe_fetch_addresses_async(host, port, allow_blocked=trusted)
        except QbittorrentError as exc:
            raise httpcore.ConnectError("unsafe torrent source URL") from exc
        # ``ConnectTimeout`` is a ``TimeoutException`` subclass, NOT a
        # ``ConnectError`` -- a broken-IPv6 dual-stack host times out on its AAAA
        # address, so catching only ``ConnectError`` would abandon the working IPv4
        # fallback. Catch both so every resolved address is actually tried.
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return await self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("unsafe torrent source URL")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = path, timeout, socket_options
        raise httpcore.UnsupportedProtocol("torrent source fetches do not use unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _SafeFetchResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for part in self._stream:
                yield part
        except _HTTP_CORE_EXCEPTIONS as exc:
            raise httpx.TransportError("torrent source request failed") from exc

    async def aclose(self) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            # Non-string cast so Callable/Awaitable are runtime-used imports
            # (a string form trips CodeQL's py/unused-import, alert #259).
            await cast(Callable[[], Awaitable[None]], aclose)()


class _SafeFetchTransport(httpx.AsyncBaseTransport):
    """Async HTTP transport for untrusted indexer/Prowlarr torrent URLs."""

    def __init__(self, trusted_host_port: tuple[str, int] | None = None) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=SafeFetchNetworkBackend(trusted_host_port=trusted_host_port),
            http1=True,
            http2=False,
            retries=0,
            max_keepalive_connections=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise httpx.TransportError("async torrent source request expected", request=request)
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            resp = await self._pool.handle_async_request(req)
        except _HTTP_CORE_EXCEPTIONS as exc:
            raise httpx.TransportError("torrent source request failed", request=request) from exc

        stream = cast(AsyncIterable[bytes], resp.stream)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_SafeFetchResponseStream(stream),
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _torrent_to_status(torrent: dict[str, object]) -> DownloadStatus:
    """Map one ``/torrents/info`` row to the port's ``DownloadStatus`` DTO.

    ``raw_state`` is kept verbatim; ``content_path`` is dropped when it merely
    echoes ``save_path`` (qBittorrent does that for not-yet-resolved torrents).
    """
    save_path = _s(torrent.get("save_path"))
    content_path = _s(torrent.get("content_path")) or None
    if (
        content_path is not None
        and save_path
        # Pure lexical compare: the intent is only to drop content_path when it
        # echoes save_path modulo a trailing slash. realpath() would do a blocking
        # per-component lstat against qBittorrent's FOREIGN filesystem namespace on
        # a reconciler/health hot path -- normpath needs no I/O and is correct here.
        and os.path.normpath(content_path) == os.path.normpath(save_path)
    ):
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
        source_client: httpx.AsyncClient | None = None,
        trusted_source_origin: str | None = None,
    ) -> None:
        self._client = client
        try:
            self._service_url = ServiceUrl.parse(base_url)
        except InvalidServiceUrl as exc:
            raise QbittorrentError("qBittorrent service URL is invalid") from exc
        self._base_url = self._service_url.base
        self._username = username
        self._password = password
        self._source_client = source_client
        # The OPERATOR-CONFIGURED Prowlarr base URL (the endpoint the app already
        # trusts with an API key for every search call), normalized to its
        # (scheme, host, port) origin. A torrent-source hop on EXACTLY this origin
        # skips the private-address SSRF veto — Prowlarr routinely serves magnetless
        # .torrent downloadUrls pointing at itself, and on the normal self-hosted
        # layout that host is 127.0.0.1 / RFC1918 / a compose alias, which the veto
        # would otherwise make ungrabbable. Matching is by URL origin, never DNS
        # results, and per hop: a redirect OFF this origin re-enters the full veto.
        self._trusted_source_origin = (
            _source_origin_triple(trusted_source_origin) if trusted_source_origin else None
        )
        if trusted_source_origin and self._trusted_source_origin is None:
            # The stored Prowlarr URL is not a usable origin (malformed IPv6
            # literal / bad port / non-http scheme — PUT /settings does not
            # URL-validate this field, so it need not be a corrupt row). The
            # CLIENT is healthy, so degrade honestly: keep the SSRF veto fully
            # closed (no trusted origin) and say so, rather than crash the
            # constructor (a 500 on every qbt-dependent route, including pure
            # DB paths) or report qBittorrent itself as unconfigured. Magnetless
            # Prowlarr-self downloadUrls will surface as per-release source
            # errors until the setting is fixed. Static message: no URL in logs.
            _logger.warning(
                "configured Prowlarr URL is not a usable trusted source origin; "
                "torrent-source fetches keep the strict SSRF veto (fix the "
                "Prowlarr URL in Settings)"
            )
        self._logged_in = False
        # qBittorrent cookies are adapter-local, never process-wide. Standard
        # cookie matching ignores ports, so a session left in the injected shared
        # client's jar could be sent to another service on the same hostname.
        self._session_cookie: tuple[str, str] | None = None
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

        Checks the full 2xx range explicitly rather than ``httpx.Response.is_error``
        (issue #87): ``is_error`` is only true for >=400, so a 3xx redirect (e.g. a
        proxy/auth redirect in front of qBittorrent) would read as success even
        though the requested operation never actually ran.
        """
        if not (_HTTP_OK <= response.status_code < _HTTP_MULTIPLE_CHOICES):
            raise QbittorrentError(f"qBittorrent request failed (HTTP {response.status_code})")

    # ---- auth ----------------------------------------------------------- #
    @staticmethod
    def _valid_session_cookie_name(name: str) -> bool:
        """Accept only qBittorrent's exact legacy/current cookie name shapes."""
        if not re.fullmatch(_SESSION_COOKIE_NAME_PATTERN, name):
            return False
        if name == "SID":
            return True
        # The regex guarantees a non-empty decimal suffix. qBittorrent derives it
        # from the WebUI listening port, whose valid range excludes zero.
        return int(name.removeprefix(_QBT_SESSION_COOKIE_PREFIX)) <= 65535

    def _discard_shared_session_cookies(self) -> None:
        """Remove qBittorrent sessions copied into the shared cookie jar.

        Production's upstream client rejects response cookies globally, but
        callers may inject an ordinary client (including tests).  Requests below
        also carry an explicit Cookie header, so the jar is never read; removing
        every legacy/current-name candidate prevents an unrelated request using
        that injected client from forwarding it by hostname-only cookie matching.
        Prefix candidates are removed even when their suffix is invalid, before
        validation raises, so a malformed response cannot leave residue behind.
        """
        for cookie in tuple(self._client.cookies.jar):
            if cookie.name != "SID" and not cookie.name.startswith(_QBT_SESSION_COOKIE_PREFIX):
                continue
            self._client.cookies.delete(cookie.name, domain=cookie.domain, path=cookie.path)

    def _session_cookie_header(self) -> dict[str, str]:
        """An explicit header suppressing every shared-client cookie."""
        if self._session_cookie is None:
            return {"Cookie": ""}
        name, value = self._session_cookie
        return {"Cookie": f"{name}={value}"}

    async def _login(self) -> None:
        """Authenticate and capture an adapter-local qBittorrent session cookie."""
        self._session_cookie = None
        self._discard_shared_session_cookies()
        try:
            response = await self._client.post(
                self._service_url.endpoint(f"{_API}/auth/login"),
                data={"username": self._username, "password": self._password},
                headers={"Referer": self._base_url, "Cookie": ""},
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            # qBittorrent unreachable during login (DNS / refused / timeout): surface
            # a retryable error rather than an opaque 500. No url/secret in the message.
            raise QbittorrentError("qBittorrent request failed") from exc
        session_cookies = [
            (cookie.name, cookie.value)
            for cookie in response.cookies.jar
            if cookie.name == "SID" or cookie.name.startswith(_QBT_SESSION_COOKIE_PREFIX)
        ]
        self._discard_shared_session_cookies()
        session_cookie = session_cookies[-1] if session_cookies else None
        validated_session_cookie: tuple[str, str] | None = None
        if session_cookie is not None:
            name, value = session_cookie
            if (
                not self._valid_session_cookie_name(name)
                or value is None
                or not re.fullmatch(_SESSION_COOKIE_VALUE_PATTERN, value)
            ):
                raise QbittorrentError("qBittorrent returned an invalid session cookie")
            validated_session_cookie = (name, value)
        self._session_cookie = validated_session_cookie
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
        try:
            url = self._service_url.endpoint(f"{_API}{path}")
        except InvalidServiceUrl as exc:
            raise QbittorrentError("qBittorrent endpoint path is invalid") from exc
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                data=data,
                files=files,
                headers=self._session_cookie_header(),
                follow_redirects=False,
            )
            if response.status_code == _HTTP_FORBIDDEN:
                self._logged_in = False
                await self._login()
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    files=files,
                    headers=self._session_cookie_header(),
                    follow_redirects=False,
                )
        except httpx.RequestError as exc:
            # qBittorrent down or network failure mid-request: surface a retryable
            # error rather than letting httpx's transport error escape as an opaque
            # 500. The message carries no url or secret.
            raise QbittorrentError("qBittorrent request failed") from exc
        return response

    # ---- add ------------------------------------------------------------ #
    async def _resolve_http_source_with_client(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str | None, bytes | None]:
        """Walk an HTTP(S) source to a magnet URI or ``.torrent`` body.

        Returns ``(magnet_uri, None)`` if a redirect leads to a magnet, or
        ``(None, torrent_bytes)`` if the URL serves a bencoded ``.torrent``.
        qBittorrent cannot follow HTTP->magnet redirects itself, hence this walk.
        """
        current = url
        for _ in range(_REDIRECT_MAX_DEPTH):
            # Per-hop trust check, by URL ORIGIN only (never DNS results): a hop on
            # exactly the operator-configured Prowlarr origin skips the private-
            # address veto (Prowlarr serves .torrent downloadUrls pointing at itself,
            # typically a private/compose address); any redirect OFF that origin --
            # including to another private host -- re-enters the full SSRF veto on
            # its own next iteration.
            if not self._is_trusted_source_url(current):
                await _assert_safe_fetch_url(current)
            try:
                async with client.stream("GET", current, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location", "")
                        if not location:
                            break
                        if location.startswith("magnet:"):
                            return location, None
                        try:
                            current = urljoin(current, location)
                        except ValueError as exc:
                            # A MALFORMED redirect Location (e.g. `http://[::1` --
                            # urljoin's urlsplit raises on the bad IPv6 literal) is
                            # attacker-suppliable indexer output: a release problem,
                            # never a raw ValueError that 500s the manual grab and
                            # aborts the whole auto-grab cycle.
                            raise QbittorrentSourceError("unsupported torrent source URL") from exc
                        continue
                    if response.status_code == _HTTP_OK:
                        content_length = response.headers.get("Content-Length")
                        if content_length is not None:
                            try:
                                declared_size = int(content_length)
                            except ValueError:
                                # A malformed Content-Length is only an early-abort
                                # optimization lost -- the streamed byte cap below
                                # still enforces _MAX_TORRENT_BYTES authoritatively.
                                declared_size = None
                            # Oversized source: a RELEASE problem (no qBittorrent
                            # request was made) -> the SourceError subtype, so the
                            # manual grab 422s and auto-grab fails just this
                            # release, never the 502 / cycle-abort a client outage
                            # earns. Same for the streamed cap below.
                            if declared_size is not None and declared_size > _MAX_TORRENT_BYTES:
                                raise QbittorrentSourceError("torrent file is too large")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > _MAX_TORRENT_BYTES:
                                raise QbittorrentSourceError("torrent file is too large")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        if body[:1] == b"d":
                            return None, body
                    break
            except httpx.RequestError as exc:
                # Indexer/Prowlarr download_url unreachable (DNS / refused / timeout):
                # a SOURCE problem -- qBittorrent was never contacted, so the
                # SourceError subtype (and an honest message; the old base-class
                # "qBittorrent request failed" blamed a healthy client). Still never
                # an opaque httpx error -> 500; no url/secret in the message. The
                # auto-grab park backoff retries a transiently-down source later.
                raise QbittorrentSourceError("torrent source request failed") from exc
        return None, None

    def _is_trusted_source_url(self, url: str) -> bool:
        """Whether ``url`` sits on the operator-configured trusted source origin."""
        if self._trusted_source_origin is None:
            return False
        return _source_origin_triple(url) == self._trusted_source_origin

    async def _resolve_http_source(self, url: str) -> tuple[str | None, bytes | None]:
        if self._source_client is not None:
            return await self._resolve_http_source_with_client(self._source_client, url)
        # The connection-time backend needs the same single allowance the per-hop
        # URL check grants: the trusted origin's host may legitimately resolve to a
        # private address. (scheme is enforced at the URL level; host+port is the
        # connection's identity.)
        trusted_host_port = (
            (self._trusted_source_origin[1], self._trusted_source_origin[2])
            if self._trusted_source_origin is not None
            else None
        )
        async with httpx.AsyncClient(
            transport=_SafeFetchTransport(trusted_host_port=trusted_host_port),
            timeout=30.0,
            trust_env=False,
        ) as client:
            return await self._resolve_http_source_with_client(client, url)

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        """Add a torrent; return its lowercased info-hash + whether it was created.

        A 409 (already present) resolves to the computed hash rather than
        erroring, reported honestly as ``created=False``: qBittorrent's 409 is
        the one signal that the torrent PREDATES this call, and the caller's
        lost-grab cleanup must never destroy (``delete_files=True``) a torrent
        it merely reused -- its data can back a live library file via hardlink
        (see :class:`~plex_manager.ports.download_client.AddResult`).

        When ``save_path`` is non-empty (a directed path -- issues #133/#157),
        the request ALSO carries ``autoTMM: "false"``: an install with global
        Automatic Torrent Management enabled otherwise ignores the per-add
        ``savepath`` field entirely and lets category/auto rules place the
        torrent, silently defeating the whole save-path direction. Sending the
        flag pins this ONE torrent to manual management so ``savepath`` is
        actually honoured, without touching the client's global AutoTMM
        setting or any other torrent. When ``save_path`` is empty (nothing to
        direct), ``autoTMM`` is omitted entirely -- the client's own
        auto-managed/manual mode for this torrent is left untouched, exactly
        the prior behaviour.
        """
        urls_value: str | None = None
        torrent_bytes: bytes | None = None
        info_hash: str | None = None

        try:
            scheme = urlparse(magnet_or_url).scheme.casefold()
        except ValueError as exc:
            # An indexer-supplied source that urlparse itself refuses (bad IPv6
            # literal) is a release problem -> the SourceError subtype, never a
            # raw ValueError escaping the taxonomy.
            raise QbittorrentSourceError("unsupported torrent source URL") from exc
        if scheme == "magnet":
            urls_value = magnet_or_url
            info_hash = _info_hash_from_magnet(magnet_or_url)
        elif scheme in ("http", "https"):
            magnet, body = await self._resolve_http_source(magnet_or_url)
            if magnet is not None:
                urls_value = magnet
                info_hash = _info_hash_from_magnet(magnet)
            elif body is not None:
                info_hash = _info_hash_from_torrent(body)
                if info_hash is None:
                    raise QbittorrentSourceError("could not determine torrent hash for HTTP source")
                torrent_bytes = body
            else:
                # Could not resolve to a magnet or locally hashable .torrent. Do
                # not ask qBittorrent to add an untrackable opaque URL.
                raise QbittorrentSourceError("could not determine torrent hash for HTTP source")
        elif scheme:
            # An unsupported scheme (ftp:// etc.) is a source veto -- nothing was
            # (or will be) handed to qBittorrent -> the SourceError subtype.
            raise QbittorrentSourceError("unsupported torrent source URL")
        else:
            urls_value = magnet_or_url
            info_hash = _info_hash_from_magnet(magnet_or_url)

        form: dict[str, str] = {"savepath": save_path, "category": category}
        if save_path:
            # A directed save path only takes effect when this torrent is NOT
            # auto-managed -- otherwise a global AutoTMM install silently
            # relocates it per category/auto rules, ignoring ``savepath``
            # entirely. Manual-manage just THIS add; omitted (below) when
            # there is no directed path, leaving the client's own mode alone.
            form["autoTMM"] = "false"
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
        # The 409 branch of _is_add_success is "already present, resolved to the
        # existing torrent" -- the honest created=False signal (every other
        # success shape means the client actually accepted a new add).
        created = response.status_code != _HTTP_CONFLICT

        if info_hash is not None:
            return AddResult(torrent_hash=info_hash.lower(), created=created)
        # No locally-derivable hash (rare: opaque .torrent URL qBit fetched). Best
        # effort: the caller can reconcile by category on the next poll.
        _logger.warning("added torrent but could not derive its info-hash locally")
        return AddResult(torrent_hash="", created=created)

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

    async def get_statuses_for_hashes(self, hashes: Sequence[str]) -> list[DownloadStatus]:
        """Return statuses for exactly the given hashes (issue #216's scoped poll).

        An empty ``hashes`` short-circuits to ``[]`` with NO request at all --
        the caller (``reconcile_and_list``) already has its own empty-rows fast
        path, but this stays defensive against any other caller. A hash qBit
        does not recognize is simply missing from its JSON array response, never
        an HTTP error, so a short result here is the honest "gone from the
        client" signal, not a failure to surface.
        """
        unique = sorted({h.lower() for h in hashes if h})
        if not unique:
            return []
        out: list[DownloadStatus] = []
        for start in range(0, len(unique), _HASHES_PER_REQUEST):
            batch = unique[start : start + _HASHES_PER_REQUEST]
            response = await self._request(
                "GET", "/torrents/info", params={"hashes": "|".join(batch)}
            )
            self._raise_for_status(response)
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

    async def get_default_save_path(self) -> str | None:
        """Return the client's GLOBAL default save path via ``GET /app/preferences``.

        Read-only diagnostic (setup/health visibility probe, issues #133/#157) --
        this adapter never posts to ``/app/setPreferences``; there is no matching
        setter on the port (see :meth:`set_location`'s docstring). A non-2xx
        response or a missing/blank ``save_path`` key returns ``None`` -- honestly
        "could not read it", never a guessed path.
        """
        response = await self._request("GET", "/app/preferences")
        if response.status_code != _HTTP_OK:
            return None
        payload = _as_dict(_decode_json(response, "/app/preferences"))
        return _s(payload.get("save_path")) or None

    async def set_location(self, info_hash: str, save_path: str) -> None:
        """Relocate ``info_hash``'s save directory via ``POST /torrents/setLocation``.

        qBittorrent moves the content asynchronously; this call only requests the
        move (surfacing a non-2xx as the usual typed :class:`QbittorrentError`) and
        returns -- it does not wait for, or otherwise confirm, completion. The
        cached ``/torrents/properties`` entry is dropped so the next
        :meth:`get_save_path` re-reads the client rather than serving the
        pre-move path for up to :data:`_PROPERTIES_TTL_SECONDS`.
        """
        response = await self._request(
            "POST",
            "/torrents/setLocation",
            data={"hashes": info_hash.lower(), "location": save_path},
        )
        self._raise_for_status(response)
        self._properties_cache.pop(info_hash.lower(), None)

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
