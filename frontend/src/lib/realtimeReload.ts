import { useSyncExternalStore } from 'react'

/**
 * A one-way latch set when the server's reported ``app_version`` changes across
 * a realtime reconnect. The `:edge` fleet auto-pulls new images (ADR-0004), so a
 * long-lived tab can outlive the build it loaded; when the reconnect sync frame
 * reports a different version we prompt the operator to reload rather than let
 * them keep driving a stale bundle against a newer API. Never un-latches on its
 * own — only a reload clears it.
 */
let reloadRequired = false
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getRealtimeReloadRequired(): boolean {
  return reloadRequired
}

export function setRealtimeReloadRequired(next: boolean): void {
  if (reloadRequired === next) return
  reloadRequired = next
  listeners.forEach((listener) => listener())
}

export function useRealtimeReloadRequired(): boolean {
  return useSyncExternalStore(subscribe, getRealtimeReloadRequired, getRealtimeReloadRequired)
}
