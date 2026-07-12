import { useSyncExternalStore } from 'react'

let realtimeConnected = false
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getRealtimeConnected(): boolean {
  return realtimeConnected
}

export function setRealtimeConnected(next: boolean): void {
  if (realtimeConnected === next) return
  realtimeConnected = next
  listeners.forEach((listener) => listener())
}

export function useRealtimeConnected(): boolean {
  return useSyncExternalStore(subscribe, getRealtimeConnected, getRealtimeConnected)
}
