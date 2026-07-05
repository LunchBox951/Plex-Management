import { useEffect, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { startRealtimeStream } from '../lib/realtime'
import { useRealtimeReloadRequired } from '../lib/realtimeReload'
import { Button } from './ui/Button'

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  useEffect(() => startRealtimeStream({ queryClient }), [queryClient])

  return (
    <>
      {children}
      <ReloadPrompt />
    </>
  )
}

/**
 * Shown when a realtime reconnect reports a different server build than the one
 * this tab loaded (the `:edge` fleet auto-pulls new images — ADR-0004). A stale
 * bundle driving a newer API is a correctness hazard, so we surface a button
 * rather than silently reload under the operator (north-star #1).
 */
function ReloadPrompt() {
  const reloadRequired = useRealtimeReloadRequired()
  if (!reloadRequired) return null
  return (
    <div
      role="alert"
      className="fixed inset-x-0 bottom-0 z-[70] flex flex-wrap items-center justify-center gap-3 border-t border-downloading/40 bg-surface px-4 py-3 shadow-2xl"
    >
      <span className="text-sm text-ink">A new version of Plex Manager is available.</span>
      <Button size="sm" onClick={() => window.location.reload()}>
        Reload
      </Button>
    </div>
  )
}
