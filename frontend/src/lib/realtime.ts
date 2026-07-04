import type { QueryClient } from '@tanstack/react-query'
import { AUTH_INVALID_EVENT } from '../api/client'
import { clearApiKey, getApiKey } from './apiKey'
import { queryKeys } from './queryClient'
import { setRealtimeConnected } from './realtimeState'

export interface RealtimeEventPayload {
  seq?: number
  topics: string[]
  reason?: string
  request_id?: number
  download_id?: number
}

export async function parseSseStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: RealtimeEventPayload) => void,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
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
}

export function startRealtimeStream({
  queryClient,
  fetchImpl = fetch,
  baseDelayMs = 1000,
  maxDelayMs = 15000,
}: RealtimeStreamOptions): () => void {
  let stopped = false
  let controller: AbortController | null = null

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
        attempt = 0
        setRealtimeConnected(true)
        await parseSseStream(response.body, (event) => applyRealtimeEvent(queryClient, event))
      } catch (err) {
        if (stopped || (err instanceof DOMException && err.name === 'AbortError')) return
      } finally {
        controller = null
        setRealtimeConnected(false)
      }

      attempt += 1
      await sleep(Math.min(maxDelayMs, baseDelayMs * 2 ** Math.min(attempt, 4)))
    }
  }

  void run()

  return () => {
    stopped = true
    controller?.abort()
    setRealtimeConnected(false)
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}
