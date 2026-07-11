import { QueryClient } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AUTH_EXPIRED_EVENT, AUTH_INVALID_EVENT } from '../api/client'
import { applyRealtimeEvent, parseSseStream, startRealtimeStream } from './realtime'
import { queryKeys } from './queryClient'
import * as apiKeyLib from './apiKey'
import * as apiKeyRotationLib from './apiKeyRotation'
import { getRealtimeReloadRequired, setRealtimeReloadRequired } from './realtimeReload'

function streamFrom(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text))
      controller.close()
    },
  })
}

function streamFromChunks(...chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk))
      controller.close()
    },
  })
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  apiKeyLib.clearApiKey()
  setRealtimeReloadRequired(false)
})

describe('parseSseStream', () => {
  it('ignores comments and parses JSON data frames split by blank lines', async () => {
    const events: unknown[] = []

    await parseSseStream(
      streamFrom(': ping\n\nevent: realtime\ndata: {"topics":["requests"]}\nid: 5\n\n'),
      (event) => events.push(event),
    )

    expect(events).toEqual([{ topics: ['requests'] }])
  })

  it('parses a CRLF frame delimiter split across network chunks', async () => {
    const events: unknown[] = []

    await parseSseStream(
      streamFromChunks('event: realtime\r\ndata: {"topics":["queue"]}\r', '\n\r', '\n'),
      (event) => events.push(event),
    )

    expect(events).toEqual([{ topics: ['queue'] }])
  })

  it('signals received bytes even for heartbeat-only comment frames', async () => {
    const events: unknown[] = []
    let bytes = 0

    await parseSseStream(
      streamFrom(': ping\n\n'),
      (event) => events.push(event),
      () => {
        bytes += 1
      },
    )

    expect(events).toEqual([])
    expect(bytes).toBe(1)
  })
})

describe('applyRealtimeEvent', () => {
  it('maps ID-free request and queue topics to existing React Query invalidations', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    applyRealtimeEvent(qc, {
      seq: 8,
      topics: ['requests', 'queue', 'settings', 'access'],
      reason: 'grab',
    })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.settings })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.plexLibraries })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.appKeyStatus })
  })

  it('maps sync and ops topics to broad refetches', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    applyRealtimeEvent(qc, { seq: 9, topics: ['sync'], reason: 'overflow' })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['blocklist'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.settings })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.plexLibraries })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.appKeyStatus })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.opsDisk })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.opsHealth })
  })

  it('invalidates Discover when a settings event arrives without a request topic', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    applyRealtimeEvent(qc, { seq: 10, topics: ['settings'], reason: 'settings_updated' })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.settings })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.plexLibraries })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: queryKeys.requests })
  })
})

function syncBody(version: string): ReadableStream<Uint8Array> {
  return streamFrom(
    `event: realtime\ndata: {"topics":["sync"],"reason":"connected","app_version":"${version}"}\n\n`,
  )
}

describe('startRealtimeStream authentication', () => {
  it('uses the same-origin session cookie when no recovery key is active', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(false)
    const getKey = vi.spyOn(apiKeyLib, 'getApiKey').mockReturnValue(null)
    const fetchImpl = vi.fn(
      async () => ({ status: 403, ok: false, body: null }) as unknown as Response,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/v1/events',
      expect.objectContaining({ credentials: 'same-origin' }),
    )
    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/v1/events',
      expect.not.objectContaining({ headers: expect.anything() }),
    )
    expect(getKey).not.toHaveBeenCalled()

    stop()
    qc.clear()
  })

  it('does not send a stored recovery key while key auth is inactive', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(false)
    const getKey = vi.spyOn(apiKeyLib, 'getApiKey').mockReturnValue('stored-but-inactive')
    const fetchImpl = vi.fn(
      async () => ({ status: 403, ok: false, body: null }) as unknown as Response,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/v1/events',
      expect.not.objectContaining({ headers: expect.anything() }),
    )
    expect(getKey).not.toHaveBeenCalled()

    stop()
    qc.clear()
  })

  it('sends the recovery key when key auth is active', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(true)
    vi.spyOn(apiKeyLib, 'getApiKey').mockReturnValue('active-key')
    const fetchImpl = vi.fn(
      async () => ({ status: 403, ok: false, body: null }) as unknown as Response,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/v1/events',
      expect.objectContaining({ headers: { 'X-Api-Key': 'active-key' } }),
    )

    stop()
    qc.clear()
  })

  it('signals an expired Plex session for a keyless 401', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(false)
    const clearKey = vi.spyOn(apiKeyLib, 'clearApiKey').mockImplementation(() => undefined)
    const dispatch = vi.spyOn(window, 'dispatchEvent')
    const fetchImpl = vi.fn(
      async () => ({ status: 401, ok: false, body: null }) as unknown as Response,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })
    await vi.advanceTimersByTimeAsync(0)

    const eventTypes = dispatch.mock.calls.map(([event]) => event.type)
    expect(eventTypes).toContain(AUTH_EXPIRED_EVENT)
    expect(eventTypes).not.toContain(AUTH_INVALID_EVENT)
    expect(clearKey).not.toHaveBeenCalled()

    stop()
    qc.clear()
  })

  it('clears and reports the current recovery key for an active-key 401', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(true)
    vi.spyOn(apiKeyLib, 'getApiKey').mockReturnValue('current-key')
    const clearKey = vi.spyOn(apiKeyLib, 'clearApiKey').mockImplementation(() => undefined)
    const dispatch = vi.spyOn(window, 'dispatchEvent')
    const fetchImpl = vi.fn(
      async () => ({ status: 401, ok: false, body: null }) as unknown as Response,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })
    await vi.advanceTimersByTimeAsync(0)

    const eventTypes = dispatch.mock.calls.map(([event]) => event.type)
    expect(clearKey).toHaveBeenCalledTimes(1)
    expect(eventTypes).toContain(AUTH_INVALID_EVENT)
    expect(eventTypes).not.toContain(AUTH_EXPIRED_EVENT)

    stop()
    qc.clear()
  })

  it('preserves a replacement key and reconnects after a stale-key 401', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(true)
    vi.spyOn(apiKeyLib, 'getApiKey').mockReturnValueOnce('old-key').mockReturnValue('new-key')
    const clearKey = vi.spyOn(apiKeyLib, 'clearApiKey').mockImplementation(() => undefined)
    const dispatch = vi.spyOn(window, 'dispatchEvent')
    const fetchImpl = vi
      .fn(async () => ({ status: 403, ok: false, body: null }) as unknown as Response)
      .mockResolvedValueOnce(({ status: 401, ok: false, body: null }) as unknown as Response)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      baseDelayMs: 1,
      maxDelayMs: 1,
    })
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(5)

    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      '/api/v1/events',
      expect.objectContaining({ headers: { 'X-Api-Key': 'new-key' } }),
    )
    expect(clearKey).not.toHaveBeenCalled()
    const eventTypes = dispatch.mock.calls.map(([event]) => event.type)
    expect(eventTypes).not.toContain(AUTH_INVALID_EVENT)
    expect(eventTypes).not.toContain(AUTH_EXPIRED_EVENT)

    stop()
    qc.clear()
  })

  it('waits for an in-flight rotation before judging an old-key 401', async () => {
    vi.useFakeTimers()
    apiKeyLib.setApiKey('old-key')
    apiKeyLib.enableApiKeyAuth()
    const finishRotation = apiKeyRotationLib.beginApiKeyRotation()
    const clearKey = vi.spyOn(apiKeyLib, 'clearApiKey')
    const dispatch = vi.spyOn(window, 'dispatchEvent')
    const fetchImpl = vi
      .fn(async () => ({ status: 403, ok: false, body: null }) as unknown as Response)
      .mockResolvedValueOnce(({ status: 401, ok: false, body: null }) as unknown as Response)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      baseDelayMs: 1,
      maxDelayMs: 1,
    })
    await vi.advanceTimersByTimeAsync(0)

    // The reconnect was rejected after the server committed, but the rotate
    // response has not delivered its replacement yet. The old key remains intact.
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(clearKey).not.toHaveBeenCalled()
    expect(dispatch.mock.calls.map(([event]) => event.type)).not.toContain(AUTH_INVALID_EVENT)

    // The rotation winner stores the new key before releasing the barrier. The
    // deferred 401 is now stale and the next reconnect uses the replacement.
    apiKeyLib.setApiKey('new-key')
    finishRotation()
    await vi.advanceTimersByTimeAsync(5)

    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      '/api/v1/events',
      expect.objectContaining({ headers: { 'X-Api-Key': 'new-key' } }),
    )
    expect(clearKey).not.toHaveBeenCalled()
    expect(dispatch.mock.calls.map(([event]) => event.type)).not.toContain(AUTH_INVALID_EVENT)

    stop()
    qc.clear()
  })

  it('stops without auth churn or reconnecting when realtime is forbidden', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(false)
    const dispatch = vi.spyOn(window, 'dispatchEvent')
    const fetchImpl = vi.fn(
      async () => ({ status: 403, ok: false, body: null }) as unknown as Response,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      baseDelayMs: 1,
      maxDelayMs: 1,
    })
    await vi.advanceTimersByTimeAsync(100)

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const eventTypes = dispatch.mock.calls.map(([event]) => event.type)
    expect(eventTypes).not.toContain(AUTH_INVALID_EVENT)
    expect(eventTypes).not.toContain(AUTH_EXPIRED_EVENT)

    stop()
    qc.clear()
  })
})

describe('startRealtimeStream version awareness', () => {
  it('prompts a reload when the server build changes across a reconnect', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(false)
    const versions = ['1.0.0', '2.0.0'] as const
    let call = 0
    const fetchImpl = vi.fn(async () => {
      const version = versions[Math.min(call, versions.length - 1)] ?? versions[1]
      call += 1
      return { status: 200, ok: true, body: syncBody(version) } as unknown as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      baseDelayMs: 10,
      maxDelayMs: 10,
    })

    // First connection: records version 1.0.0, no reload yet.
    await vi.advanceTimersByTimeAsync(0)
    expect(getRealtimeReloadRequired()).toBe(false)

    // Backoff, then the second connection reports 2.0.0 -> reload latched.
    await vi.advanceTimersByTimeAsync(50)
    expect(fetchImpl.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(getRealtimeReloadRequired()).toBe(true)

    stop()
    qc.clear()
  })
})

describe('startRealtimeStream backoff', () => {
  it('escalates reconnect backoff when the stream dies right after headers', async () => {
    // A proxy/server that returns 200 then closes the body immediately must NOT
    // pin the reconnect loop at the base delay: backoff resets only after a
    // connection outlives the stability window, so this flap escalates instead.
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(false)
    let clock = 0
    const fetchTimes: number[] = []
    const fetchImpl = vi.fn(async () => {
      fetchTimes.push(clock)
      // 200 with a body that ends at once (one heartbeat comment, then EOF).
      return { status: 200, ok: true, body: streamFrom(': ping\n\n') } as unknown as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      baseDelayMs: 1000,
      maxDelayMs: 60000,
      // Every connection lives ~0ms (body closes instantly), always below this,
      // so no cycle is ever treated as "healthy".
      stabilityWindowMs: 10000,
      now: () => clock,
    })

    // Drive many reconnect cycles, keeping the injected clock in lockstep with
    // the fake timers so `now()` tracks wall time.
    for (let t = 0; t <= 40000; t += 250) {
      clock = t
      await vi.advanceTimersByTimeAsync(250)
    }

    const gaps = fetchTimes.slice(1).map((t, i) => t - (fetchTimes[i] ?? 0))
    expect(gaps.length).toBeGreaterThanOrEqual(3)
    // The delay climbs past the base tier instead of looping at ~2s forever.
    expect(gaps[gaps.length - 1]).toBeGreaterThan(gaps[0] ?? 0)
    expect(Math.max(...gaps)).toBeGreaterThanOrEqual(7000)

    stop()
    qc.clear()
  })
})

describe('startRealtimeStream watchdog', () => {
  it('aborts and reconnects when the stream goes silent past the threshold', async () => {
    vi.useFakeTimers()
    vi.spyOn(apiKeyLib, 'isApiKeyAuthEnabled').mockReturnValue(false)
    let clock = 0
    const fetchImpl = vi.fn(async (_url: unknown, opts: { signal: AbortSignal }) => {
      let streamController: ReadableStreamDefaultController<Uint8Array> | null = null
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller
        },
      })
      // A real fetch aborts the body read when the signal fires; emulate that so
      // the parser's pending read rejects and the run loop reconnects.
      opts.signal.addEventListener('abort', () => {
        try {
          streamController?.error(new DOMException('aborted', 'AbortError'))
        } catch {
          /* stream already closed */
        }
      })
      return { status: 200, ok: true, body } as unknown as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const stop = startRealtimeStream({
      queryClient: qc,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      baseDelayMs: 10,
      maxDelayMs: 10,
      heartbeatMs: 1000,
      watchdogMs: 2500,
      now: () => clock,
    })

    // First (silent) connection is established.
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchImpl).toHaveBeenCalledTimes(1)

    // Jump the clock past the stale threshold; the next watchdog tick aborts.
    clock = 3000
    await vi.advanceTimersByTimeAsync(1000)
    // Abort -> reconnect after backoff.
    await vi.advanceTimersByTimeAsync(50)
    expect(fetchImpl.mock.calls.length).toBeGreaterThanOrEqual(2)

    stop()
    qc.clear()
  })
})
