# ADR-0017: Realtime SSE invalidations over a permanent polling floor

- **Status:** Accepted
- **Date:** 2026-07-04
- **Context builds on:** [ADR-0004](0004-edge-stable-release-channels.md)
  (`:edge`/`:stable` promotion — long-lived tabs can outlive their build),
  [ADR-0005](0005-zero-terminal-web-operability.md) (web-operability — the UI must
  feel live without an operator poking it), [ADR-0009](0009-frontend-typed-spa.md)
  (the typed SPA + React Query cache these events invalidate).

> This ADR may be renumbered at merge time if a sibling PR lands an ADR with the
> same number first; the decision content is unaffected.

## Context

The SPA's live surfaces (`/queue`, `/requests`, the Status cards) were polled on
fixed intervals (2s/5s/15s — ADR-0009 left a note that "when the backend grows
SSE… the intervals go away"). Polling is simple and self-healing but trades
latency against load: to feel live it must poll fast, and fast polling costs a
request per surface per tick per open tab, forever.

We want push-style freshness without importing a stateful realtime framework or
a second protocol surface. Three things shaped the design, each borrowed from a
battle-tested stack rather than re-derived (north-star #4):

- **Overseerr never stops polling.** Even with its websocket connected it keeps a
  slow poll as a safety net. A push channel that is trusted as the *only* path to
  freshness is a single point of failure: a zombied stream (TCP half-open, a
  proxy that silently dropped the connection, a missed heartbeat) leaves the UI
  frozen with no error to show.
- **Radarr gates publishes on `IsConnected`.** It does no signalr work when
  nobody is listening.
- **`:edge` auto-pulls new images (ADR-0004).** A tab open across a rolling image
  swap is running a bundle older than the API it is talking to.

## Decision

Add an authenticated `GET /api/v1/events` **Server-Sent Events** stream that
carries **coarse cache-invalidation hints**, backed by an in-process hub — and
keep polling as a permanent, slowed-down safety net rather than removing it.

**SSE, not WebSocket.** The traffic is strictly server→client and low-volume;
SSE is one long-lived `GET` over plain HTTP/1.1 with auto-reconnect semantics,
no second protocol, no upgrade handshake, and it flows through the same auth and
proxy path as every other request. A bidirectional socket buys nothing here.

**Coarse invalidation, not payload push.** Events name *topics*
(`requests`, `queue`, `blocklist`, `ops:disk`, …), never row state. The client
invalidates the matching React Query keys and refetches the existing typed DTOs.
The REST endpoints stay the single source of truth (ADR-0009's contract is
untouched), reconnect/overflow collapse to one broad `sync`, and there is no
second, drift-prone serialization of domain state to keep in sync.

**A permanent polling floor (the load-bearing safety net).** When the stream is
connected the client does **not** stop polling — it drops to a slow floor
(queue ~25s, requests ~45s) instead of the fast cadence (2s/5s). A dead or
zombied stream therefore self-heals within one slow tick **regardless of whether
the client-side watchdog fires**. Push is the optimization; polling is the
guarantee.

**Belt-and-braces client watchdog.** The stream server-heartbeats every 15s; the
client surfaces a received-bytes signal on *every* frame (including heartbeat
comment frames) and, if it sees silence beyond ~2.5× the heartbeat (~38s),
aborts and reconnects. The floor already covers correctness; the watchdog just
shortens the healing window in the common case.

**In-process, single-worker hub (with a documented scale-out path).** The hub is
a bounded in-memory fanout: each subscriber gets a small queue; on overflow the
subscriber collapses to a single `sync` rather than blocking a publisher or
growing without bound. Publishes short-circuit when there are no subscribers.
This is correct **only under a single worker** — a second worker would fan out
only to the clients pinned to the publishing process. That is an accepted
constraint for the single-container deployment (ADR-0003); startup logs a loud
WARNING when a multi-worker configuration is detectable, and the polling floor
still heals any client on a sibling worker. The scale-out path, when needed, is
to replace the in-process hub with a shared broker / durable outbox behind the
same `publish_realtime` seam — no call-site changes.

**No DB session held for the stream's lifetime.** Auth validates the API key
against a session opened and closed *before* streaming begins; the long-lived
connection then holds no database connection, so open tabs cannot exhaust the
small aiosqlite pool shared with the reconcile/autograb/eviction workers.

**Version-aware reload.** The connect-time `sync` carries the server build; a
reconnect reporting a different build (an `:edge` swap under a long-lived tab)
prompts the operator to reload rather than silently driving a stale bundle
against a newer API — surfaced as a button, never an automatic reload
(north-star #1).

## Consequences

- Live surfaces update within the push latency in the common case, while steady-
  state request volume drops by roughly an order of magnitude per open tab (fast
  poll → slow floor) once the stream is up.
- One new unauthenticated-until-validated streaming endpoint and one in-process
  singleton (`app.state.realtime_hub`); no schema change, no new dependency
  beyond the FastAPI floor bump for `fastapi.sse`.
- **Transport hygiene is required end to end.** `Cache-Control: no-cache` and
  `X-Accel-Buffering: no` are set on the response (FastAPI's SSE layer sets both);
  event-stream responses must be excluded from any future compression middleware
  (none is installed today); and any reverse proxy must disable response
  buffering (`proxy_buffering off;` for nginx). A buffering proxy defeats SSE
  silently — the polling floor is what keeps that from becoming an outage.
- The single-worker invariant is now load-bearing and must be honored by the
  deployment until the scale-out path is taken.

## Alternatives considered

- **WebSocket.** Rejected: bidirectional, second protocol + upgrade path, no
  benefit for server→client-only, low-volume signals.
- **Push full row payloads over the stream.** Rejected: a second serialization of
  domain state that would drift from the REST DTOs and re-introduce the exact
  consistency problem the "REST is the source of truth" rule avoids.
- **Drop polling once SSE connected.** Rejected — this is the Overseerr lesson: a
  trusted-but-dead stream then freezes the UI with nothing to show. The slow
  floor is cheap and makes the whole feature fail-safe.
- **Shared broker (Redis/NATS) now.** Deferred: unjustified operational weight for
  a single-container app; the `publish_realtime` seam keeps it a drop-in later.
