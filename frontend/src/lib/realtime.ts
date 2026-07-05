import type { QueryClient } from '@tanstack/react-query'
import { AUTH_INVALID_EVENT } from '../api/client'
import { clearApiKey, getApiKey } from './apiKey'
import { queryKeys } from './queryClient'
import { setRealtimeConnected } from './realtimeState'
import { setRealtimeReloadRequired } from './realtimeReload'

export interface RealtimeEventPayload {
  seq?: number
  topics: string[]
  reason?: string
  request_id?: number
  download_id?: number
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

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    onBytes?.()
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      emitFrame(frame, onEvent)
      boundary = buffer.indexOf('\n\n')
    }
  }

  buffer += decoder.decode()
  if (buffer.trim().length > 0) {
    emitFrame(buffer, onEvent)
  }
}

function emitFrame(frame: string, onEvent: (event: RealtimeEventPayload) => void): void {
  const data: string[] = []
  for (const rawLine of frame.split('\n')) {
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
  const requestId = event.request_id

  if (topics.has('sync')) {
    void qc.invalidateQueries({ queryKey: queryKeys.requests })
    void qc.invalidateQueries({ queryKey: queryKeys.queue })
    void qc.invalidateQueries({ queryKey: ['discover'] })
    void qc.invalidateQueries({ queryKey: ['blocklist'] })
    void qc.invalidateQueries({ queryKey: queryKeys.opsDisk })
    void qc.invalidateQueries({ queryKey: queryKeys.opsHealth })
  }
  if (topics.has('requests')) {
    void qc.invalidateQueries({ queryKey: queryKeys.requests })
    if (typeof requestId === 'number') {
      void qc.invalidateQueries({ queryKey: queryKeys.request(requestId) })
    }
    void qc.invalidateQueries({ queryKey: ['discover'] })
  }
  if (topics.has('queue')) {
    void qc.invalidateQueries({ queryKey: queryKeys.queue })
    if (typeof requestId === 'number') {
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
      void qc.invalidateQueries({ queryKey: queryKeys.request(requestId) })
      void qc.invalidateQueries({ queryKey: ['discover'] })
    }
  }
  if (topics.has('blocklist')) {
    void qc.invalidateQueries({ queryKey: ['blocklist'] })
  }
  if (topics.has('ops:disk')) {
    void qc.invalidateQueries({ queryKey: queryKeys.opsDisk })
  }
  if (topics.has('ops:health')) {
    void qc.invalidateQueries({ queryKey: queryKeys.opsHealth })
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
      const key = getApiKey()
      if (!key) {
        setRealtimeConnected(false)
        await sleep(baseDelayMs)
        continue
      }

      controller = new AbortController()
      let connectedAt: number | null = null
      try {
        const response = await fetchImpl('/api/v1/events', {
          headers: { 'X-Api-Key': key },
          signal: controller.signal,
        })
        if (response.status === 401) {
          if (key === getApiKey()) {
            clearApiKey()
            window.dispatchEvent(new Event(AUTH_INVALID_EVENT))
          }
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
