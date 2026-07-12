# ADR-0018: Origin-confined configured service URLs

- **Status:** Accepted — 2026-07-11
- **Deciders:** LunchBox951 (owner)
- **Context builds on:** [ADR-0005](0005-zero-terminal-web-operability.md)
  (service configuration stays web-operable),
  [ADR-0006](0006-download-client-port-qbittorrent.md) (qBittorrent is the v1
  download adapter), and [ADR-0016](0016-plex-oauth-owner-sessions.md) (Plex
  owner sign-in and the stored OAuth token used by setup).
- **Addresses:** CodeQL `py/partial-ssrf` alerts 281–285. The same boundary is
  applied to Prowlarr, including the risk previously recorded on alert 247.

## Context

Plex Manager makes backend HTTP requests to three operator-configured services:
Plex, Prowlarr, and qBittorrent. Those services commonly live on loopback,
RFC1918 LAN addresses, Docker DNS names, or behind a reverse-proxy path prefix.
The usual SSRF rule of allowing only globally routable destinations would make
normal self-hosted deployments unusable.

The old adapters kept each base as a string and appended API paths with string
concatenation. Shape validation limited persisted values to HTTP(S), but the
adapter boundary itself did not prove that URL parsing could not reinterpret an
authority or normalize an attacker-controlled prefix. In addition, a shared
`httpx.AsyncClient` configured to follow redirects could forward Plex,
Prowlarr, or qBittorrent credentials to a redirect target.

There was a separate credential-egress problem. Setup could send the signed-in
owner's stored Plex OAuth token to a submitted URL before the response's machine
identifier was ownership-checked. Settings also reused a masked or omitted
stored secret after a service URL changed, so a URL-only destination change
could send an existing Plex token, Prowlarr API key, or qBittorrent password to
the new destination.

## Decision

Treat configured service URLs as a distinct, explicitly private-network-capable
trust boundary rather than as general-purpose or untrusted fetch URLs.

### Parse once and compose beneath one origin

All Plex, Prowlarr, and qBittorrent adapters construct requests through a shared
`ServiceUrl` value object:

- the base must fully match an explicit HTTP(S) allowlist and parse in `httpx`;
- URL userinfo, query/fragment state, backslashes, percent escapes, control
  characters, ambiguous empty segments, and `.` / `..` path segments are
  rejected;
- a safe ASCII reverse-proxy path prefix is retained; and
- adapter-owned endpoint paths are separately allowlisted and installed with
  parsed-URL component replacement, so an endpoint cannot alter the configured
  scheme, host, or port.

The positive full-string restriction is both the runtime invariant and an
explicit boundary visible to static analysis. Web validation calls the same
parser before persistence, while adapters call it again so corrupt or legacy
stored values fail closed.

Every credential-bearing configured-service request explicitly disables
redirect following. Redirects are reported as upstream failures and never get a
second request carrying credentials to another origin.

The process-wide upstream HTTP client rejects response cookies. qBittorrent's
session cookie (`SID` on older releases and `QBT_SID_<port>` on 5.2+) is captured
by its adapter, removed from any injected shared jar, and sent through an
explicit Cookie header only to that adapter's confined endpoints. This matters
because standard cookie matching includes the hostname and path but not the
port; a shared jar could otherwise forward the session to another service on the
same host.

### Stored credentials do not silently cross origins

During Plex-first setup, using the signed-in owner's stored OAuth token requires
the submitted base URL to exactly match a connection advertised by plex.tv for
one of that owner's servers. This check happens before the token is sent. The
documented custom-URL path remains available when the operator explicitly
supplies the service token; the returned machine identifier must still belong to
the signed-in owner before setup can complete.

In post-init settings, changing a configured URL's scheme, host, or effective
port or reverse-proxy prefix requires explicit re-entry of the corresponding
Plex token, Prowlarr API key, or qBittorrent password. Only an exact canonical
base match (normalizing host case, default ports, and a trailing slash) may reuse
the stored credential: different paths on one origin can route to different
backends. The check happens before a Plex verification probe
or any settings write, so a rejected update neither discloses a secret nor leaves
partial configuration behind.

The supported single-process deployment serializes that complete settings
validation, probe, and commit sequence. Concurrent partial updates therefore
cannot validate a secret against the old base and later commit it alongside a
different request's newly written base. A future multi-worker deployment must
replace the in-process guard with an equivalent database-level version or lock.

### Keep untrusted fetches on their stronger policy

This decision does not replace the separate policy for torrent-source URLs
returned by an indexer. Those are untrusted fetch targets rather than
operator-configured services and remain subject to global-IP checks, DNS pinning,
and per-hop redirect validation. The two URL classes must not share a policy:
permitting private networks is necessary for configured services and unsafe for
untrusted content fetches.

## Consequences

- Normal loopback, LAN, Docker-DNS, IPv6, and safe reverse-proxy-prefix
  deployments remain supported.
- Ambiguous or unusually encoded base paths must be replaced with an unreserved
  ASCII path prefix. This is an intentional compatibility restriction at a
  security boundary.
- Any service destination change, including a reverse-proxy path change, asks
  the operator for its secret again. Pure canonicalization differences do not.
- An authenticated administrator can still deliberately configure a private
  destination. That is required product behavior, not an anonymous arbitrary-URL
  fetch surface; destination authorization is enforced by admin/setup ownership
  gates and explicit credential consent.
- There is no database migration and no request/response shape change. The new
  `credential_reentry_required` value uses the existing structured error
  envelope.
