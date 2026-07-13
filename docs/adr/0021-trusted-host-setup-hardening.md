# ADR-0021: Trusted-Host validation on the setup flow

- **Status:** Accepted — 2026-07-12
- **Deciders:** LunchBox951 (owner)
- **Context builds on:** [ADR-0005](0005-zero-terminal-web-operability.md)
  (zero-terminal, web-operable — the new knob must have a safe default and never
  demand a terminal step for a normal install), [ADR-0016](0016-plex-oauth-owner-sessions.md)
  (the pre-init `/api/v1/auth/plex` first-owner claim this hardens), and
  [ADR-0018](0018-origin-confined-service-urls.md) (the project's other recent
  trust-boundary ADR — configured *outbound* service URLs; this one is the
  *inbound* counterpart).

## Context

Plex Manager's only defense against a network-originated first-owner setup claim
was its loopback bind (`PLEX_MANAGER_HOST=127.0.0.1` by default; ADR-0016 treats
loopback as preventing network claims). Nothing validated the inbound HTTP `Host`
header. A DNS-rebinding origin — a public DNS name whose resolution flips to
`127.0.0.1` after the browser has already loaded a page from it — reaches the app
as a same-origin loopback connection, skipping CORS, while carrying an
attacker-chosen `Host`. Because `SetupGuardMiddleware` deliberately allowlists
`/api/v1/auth` and `/api/v1/setup` pre-init (sign-in IS the first setup step), that
request can win the exclusive first-owner claim and lock out the real operator.

## Decision

Install a `TrustedHostMiddleware` (`web/trusted_host.py`) as the **outermost**
layer in `create_app()` — it wraps `SetupGuardMiddleware` and every route, so an
untrusted `Host` is rejected (`400 invalid_host`) before any allowlisted path,
guarded path, or query runs. It stays installed unconditionally, not only while
uninitialized: the setup claim is the sharpest edge, but there is no reason to
leave the boundary open once initialized either.

### Trust policy

A `Host` (lowercased, trailing dot and port stripped) is trusted iff:

1. it is a configured hostname (`Settings.allowed_hosts`, new
   `PLEX_MANAGER_ALLOWED_HOSTS`) or the literal `localhost`; or
2. it parses as an IP literal that is loopback, private (RFC1918 / IPv6 ULA), or
   link-local.

(2) is the load-bearing default-safety call: a browser only ever emits
`Host: <ip literal>` when its URL's host *is* that literal, meaning it dialed
that address directly. There is no hostname indirection to rebind an IP literal
to — a rebinding attack requires a DNS name the attacker controls resolution
for, and IP literals have no DNS lookup step at all. So trusting
loopback/private/link-local IP literals by default costs nothing against this
threat while keeping every existing zero-config topology working: bare-metal
loopback (the shipped default), SSH-tunnel (`localhost`), and LAN-by-IP.
Reverse-proxy installs that forward a public hostname set
`PLEX_MANAGER_ALLOWED_HOSTS` at install time — the same tier as the existing
`PLEX_MANAGER_HOST` / `PLEX_MANAGER_TRUSTED_PROXY_HOPS` knobs, not a
use-time or recovery-time terminal step, so ADR-0005's north star holds. A `*`
sentinel disables the check entirely for topologies that genuinely don't fit —
documented as discouraged, kept so no deployment shape hits a dead end.

Only the raw `Host` header is ever read. `X-Forwarded-Host` (or any other
forwarded-host header) is never consulted here: it is attacker-suppliable, and
trusting it would let a rebinding request simply forward a trusted value while
keeping the untrusted real `Host` — reintroducing the exact bypass this ADR
closes. A reverse proxy that terminates a public hostname sets the *outbound*
`Host` header itself (or its origin is added to `allowed_hosts`); it does not
need this middleware to trust a forwarded-host claim.

### Complements, does not replace, the setup token

`PLEX_MANAGER_SETUP_TOKEN` (ADR-0016) remains optional defense-in-depth,
unchanged and enforced independently of this middleware — a trusted-Host
request still needs a matching `X-Setup-Token` when one is configured. Host
validation closes the origin-confusion vector; the setup token (when an operator
opts in) adds a bearer-secret requirement on top.

## Consequences

- Any install reached through a reverse proxy on a **public hostname** must set
  `PLEX_MANAGER_ALLOWED_HOSTS` to that hostname, or every request now gets
  `400 invalid_host`. Loopback, SSH-tunnel, and LAN-by-IP access are unaffected
  by default.
- Reverse proxies must **preserve the client's original `Host`** on the
  upstream request (nginx: `proxy_set_header Host $host;` — nginx's default is
  `$proxy_host`, which rewrites `Host` to the private upstream address). A
  proxy that rewrites every `Host` to its upstream makes each proxied request
  arrive with an always-trusted private literal, so host validation is then
  effectively delegated to the proxy's own vhost matching instead of enforced
  in-app. This is a documented deployment requirement (the env example file
  and the middleware's module docstring both carry it): the app cannot
  distinguish a proxy-rewritten `Host` from a genuine direct loopback/LAN
  request at this layer.
- The 400 body carries a fixed `invalid_host` detail only — the untrusted `Host`
  value is never echoed back or logged, consistent with north star #3 (surface
  states honestly, never leak secrets/attacker-controlled input into responses).
