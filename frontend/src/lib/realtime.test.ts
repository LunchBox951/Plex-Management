import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { applyRealtimeEvent, parseSseStream } from './realtime'
import { queryKeys } from './queryClient'

function streamFrom(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text))
      controller.close()
    },
  })
}

describe('parseSseStream', () => {
  it('ignores comments and parses JSON data frames split by blank lines', async () => {
    const events: unknown[] = []

    await parseSseStream(
      streamFrom(': ping\n\nevent: realtime\ndata: {"topics":["requests"],"request_id":7}\nid: 5\n\n'),
      (event) => events.push(event),
    )

    expect(events).toEqual([{ topics: ['requests'], request_id: 7 }])
  })
})

describe('applyRealtimeEvent', () => {
  it('maps request and queue topics to existing React Query invalidations', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    applyRealtimeEvent(qc, {
      seq: 8,
      topics: ['requests', 'queue'],
      reason: 'grab',
      request_id: 7,
      download_id: 3,
    })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.request(7) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
  })

  it('maps sync and ops topics to broad refetches', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    applyRealtimeEvent(qc, { seq: 9, topics: ['sync'], reason: 'overflow' })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['blocklist'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.opsDisk })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.opsHealth })
  })
})
