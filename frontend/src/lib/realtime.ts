import type { QueryClient } from '@tanstack/react-query'
import { AUTH_EXPIRED_EVENT, AUTH_INVALID_EVENT } from '../api/client'
import { clearApiKey, getApiKey, isApiKeyAuthEnabled } from './apiKey'
import { getPendingApiKeyRotation } from './apiKeyRotation'
import { queryKeys } from './queryClient'
import { setRealtimeConnected } from './realtimeState'
import { setRealtimeReloadRequired } from './realtimeReload'

export interface RealtimeEventPayload {
  seq?: number
  topics: string[]
  reason?: string
  app_version?: string
}

/**
 * Parse an SSE byte stream into typed events.
 *
 * ``onBytes`` fires on EVERY received chunk — including heartbeat comment frames
 * (``: ping``) and any other non-``data`` frames the parser otherwise discards.
 * The reconnect watchdog relies on this: a healthy but idle stream still emits a
 * server heartbeat every ~15s, so a silence longer than that means the
 * connection is dead/zombied even though no application event was due.
 */
export async function parseSseStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: RealtimeEventPayload) => void,
  onBytes?: () => void,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let pendingCr = false

  const normalizeChunk = (decoded: string, final = false): string => {
    let text = (pendingCr ? '\r' : '') + decoded
    pendingCr = false
    // A CRLF terminator may straddle network chunks. Hold a trailing CR until
    // the next decode so it is never mistaken for a standalone line ending.
    if (!final && text.endsWith('\r')) {
      pendingCr = true
      text = text.slice(0, -1)
    }
    return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    onBytes?.()
    buffer += normalizeChunk(decoder.decode(value, { stream: true }))
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      emitFrame(frame, onEvent)
      boundary = buffer.indexOf('\n\n')
    }
  }

  buffer += normalizeChunk(decoder.decode(), true)
  if (buffer.trim().length > 0) {
    emitFrame(buffer, onEvent)
  }
}

function emitFrame(frame: string, onEvent: (event: RealtimeEventPayload) => void): void {
  const data: string[] = []
  for (const rawLine of frame.split(/\r\n|\n|\r/)) {
    if (rawLine.length === 0 || rawLine.startsWith(':')) continue
    const colon = rawLine.indexOf(':')
    const field = colon === -1 ? rawLine : rawLine.slice(0, colon)
    let value = colon === -1 ? '' : rawLine.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') data.push(value)
  }
  if (data.length === 0) return
  const parsed: unknown = JSON.parse(data.join('\n'))
  if (isRealtimeEvent(parsed)) onEvent(parsed)
}

function isRealtimeEvent(value: unknown): value is RealtimeEventPayload {
  return (
    typeof value === 'object' &&
    value !== null &&
    'topics' in value &&
    Array.isArray((value as { topics: unknown }).topics) &&
    (value as { topics: unknown[] }).topics.every((topic) => typeof topic === 'string')
  )
}

export function applyRealtimeEvent(qc: QueryClient, event: RealtimeEventPayload): void {
  const topics = new Set(event.topics)

  if (topics.has('sync')) {
    void qc.invalidateQueries({ queryKey: queryKeys.requests })
    void qc.invalidateQueries({ queryKey: queryKeys.queue })
    void qc.invalidateQueries({ queryKey: ['discover'] })
    void qc.invalidateQueries({ queryKey: ['blocklist'] })
    void qc.invalidateQueries({ queryKey: queryKeys.settings })
    void qc.invalidateQueries({ queryKey: queryKeys.plexLibraries })
    void qc.invalidateQueries({ queryKey: queryKeys.appKeyStatus })
    void qc.invalidateQueries({ queryKey: queryKeys.opsDisk })
    void qc.invalidateQueries({ queryKey: queryKeys.opsHealth })
    void qc.invalidateQueries({ queryKey: queryKeys.updateStatus })
  }
  if (topics.has('requests')) {
    void qc.invalidateQueries({ queryKey: queryKeys.requests })
    void qc.invalidateQueries({ queryKey: ['discover'] })
  }
  if (topics.has('queue')) {
    void qc.invalidateQueries({ queryKey: queryKeys.queue })
  }
  if (topics.has('blocklist')) {
    void qc.invalidateQueries({ queryKey: ['blocklist'] })
  }
  if (topics.has('settings')) {
    void qc.invalidateQueries({ queryKey: queryKeys.settings })
    void qc.invalidateQueries({ queryKey: queryKeys.plexLibraries })
    // Match the local settings mutation: TMDB credentials back every Discover
    // cache, and Discover has no polling cadence to heal another tab on its own.
    void qc.invalidateQueries({ queryKey: ['discover'] })
  }
  if (topics.has('access')) {
    void qc.invalidateQueries({ queryKey: queryKeys.appKeyStatus })
  }
  if (topics.has('ops:disk')) {
    void qc.invalidateQueries({ queryKey: queryKeys.opsDisk })
  }
  if (topics.has('ops:health')) {
    void qc.invalidateQueries({ queryKey: queryKeys.opsHealth })
  }
  if (topics.has('updates')) {
    void qc.invalidateQueries({ queryKey: queryKeys.updateStatus })
  }
}

export interface RealtimeStreamOptions {
  queryClient: QueryClient
  fetchImpl?: typeof fetch
  baseDelayMs?: number
  maxDelayMs?: number
  /** Server heartbeat cadence; the watchdog checks at roughly this interval. */
  heartbeatMs?: number
  /** Stale threshold: abort + reconnect after this much silence (~2.5x heartbeat). */
  watchdogMs?: number
  /**
   * Minimum lifetime a connection must reach before it counts as "healthy" and is
   * allowed to reset the reconnect backoff. Without it, backoff would reset on the
   * bare HTTP 200 — so a proxy/server that accepts the request then closes the
   * body immediately (or right after the sync frame) pins the reconnect loop at
   * the base delay forever instead of escalating. Defaults to one heartbeat
   * interval: a stream that outlives a heartbeat is genuinely established.
   */
  stabilityWindowMs?: number
  /** Injectable clock for tests. */
  now?: () => number
}

export function startRealtimeStream({
  queryClient,
  fetchImpl = fetch,
  baseDelayMs = 1000,
  maxDelayMs = 15000,
  heartbeatMs = 15000,
  watchdogMs = heartbeatMs * 2.5,
  stabilityWindowMs = heartbeatMs,
  now = () => Date.now(),
}: RealtimeStreamOptions): () => void {
  let stopped = false
  let controller: AbortController | null = null
  let watchdogTimer: ReturnType<typeof window.setInterval> | null = null
  let lastByteAt = now()
  // Persists across reconnect iterations: the first sync frame records the
  // server build; a later reconnect reporting a different build means the image
  // was rolled (ADR-0004) under a long-lived tab, so we prompt a reload.
  let knownVersion: string | undefined

  function clearWatchdog(): void {
    if (watchdogTimer !== null) {
      window.clearInterval(watchdogTimer)
      watchdogTimer = null
    }
  }

  function armWatchdog(): void {
    clearWatchdog()
    watchdogTimer = window.setInterval(() => {
      if (now() - lastByteAt > watchdogMs) {
        // Abort WITHOUT setting `stopped`: the run loop's catch falls through to
        // backoff + reconnect, and the polling floor covers the gap meanwhile.
        controller?.abort()
      }
    }, heartbeatMs)
  }

  function noteVersion(event: RealtimeEventPayload): void {
    const version = event.app_version
    if (typeof version !== 'string') return
    if (knownVersion === undefined) {
      knownVersion = version
    } else if (knownVersion !== version) {
      setRealtimeReloadRequired(true)
    }
  }

  async function run(): Promise<void> {
    let attempt = 0
    while (!stopped) {
      // Match the typed REST client's credential selection exactly: the recovery
      // key is opt-in per tab; otherwise this same-origin request relies on the
      // normal HTTP-only Plex session cookie.
      const key = isApiKeyAuthEnabled() ? getApiKey() : null

      controller = new AbortController()
      let connectedAt: number | null = null
      try {
        const response = await fetchImpl('/api/v1/events', {
          credentials: 'same-origin',
          ...(key ? { headers: { 'X-Api-Key': key } } : {}),
          signal: controller.signal,
        })
        if (response.status === 401) {
          if (key === null) {
            window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
            return
          }
          // Rotation commits before its HTTP response can hand this tab the new
          // key, and the commit deliberately closes old-key streams. If that EOF
          // reconnects quickly, defer judging its 401 until the local rotation
          // settles. Success stores the replacement before releasing the barrier;
          // failure leaves the old key current and therefore genuinely invalid.
          let rotation = getPendingApiKeyRotation(key)
          while (rotation !== null) {
            await rotation
            rotation = getPendingApiKeyRotation(key)
          }
          if (isApiKeyAuthEnabled() && key === getApiKey()) {
            clearApiKey()
            window.dispatchEvent(new Event(AUTH_INVALID_EVENT))
            return
          }
          // A slow 401 for an old/disabled key says nothing about the current
          // credential. Reconnect using the now-current auth after backoff.
          throw new Error('stale realtime credential rejected')
        }
        if (response.status === 403) {
          // Realtime is admin-only so coarse queue/blocklist/activity signals
          // never leak across shared users. Their normal disconnected polling
          // cadence stays active and is the intended transport.
          return
        }
        if (!response.ok || response.body === null) {
          throw new Error(`realtime stream failed: ${response.status}`)
        }
        setRealtimeConnected(true)
        connectedAt = now()
        lastByteAt = connectedAt
        armWatchdog()
        await parseSseStream(
          response.body,
          (event) => {
            noteVersion(event)
            applyRealtimeEvent(queryClient, event)
          },
          () => {
            lastByteAt = now()
          },
        )
      } catch {
        // Network error, a watchdog abort, or a teardown abort. Teardown sets
        // `stopped` (handled below); everything else falls through to reconnect.
      } finally {
        clearWatchdog()
        controller = null
        setRealtimeConnected(false)
      }

      if (stopped) return
      // Reset backoff only once a connection has PROVEN healthy — i.e. it stayed
      // live for at least the stability window. Resetting on the bare HTTP 200
      // (before the body is consumed) would let an "accept-then-immediately-close"
      // proxy pin this loop at the base delay indefinitely, re-opening a server DB
      // session (require_api_key_short_session) every couple of seconds per tab; a
      // stream that flaps that fast now escalates its backoff like any other.
      if (connectedAt !== null && now() - connectedAt >= stabilityWindowMs) {
        attempt = 0
      }
      attempt += 1
      await sleep(Math.min(maxDelayMs, baseDelayMs * 2 ** Math.min(attempt, 4)))
    }
  }

  void run()

  return () => {
    stopped = true
    clearWatchdog()
    controller?.abort()
    setRealtimeConnected(false)
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}
