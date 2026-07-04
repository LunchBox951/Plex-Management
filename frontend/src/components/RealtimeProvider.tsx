import { useEffect, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { startRealtimeStream } from '../lib/realtime'

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  useEffect(() => startRealtimeStream({ queryClient }), [queryClient])

  return <>{children}</>
}
