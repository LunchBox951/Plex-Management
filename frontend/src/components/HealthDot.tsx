import { useQuery } from '@tanstack/react-query'
import { client } from '../api/client'
import { cn } from '../lib/cn'

/** Tiny liveness indicator in the header — green when /health answers, red when not. */
export function HealthDot() {
  const { data, isError, isPending } = useQuery({
    queryKey: ['health'],
    queryFn: async () => {
      const { data, error } = await client.GET('/health')
      if (error) throw new Error('unhealthy')
      return data
    },
    refetchInterval: 15000,
    retry: false,
  })

  // Don't report 'offline' before the first response has settled — that would
  // flag a healthy server as down while it is merely loading.
  const state: 'checking' | 'online' | 'offline' = isPending
    ? 'checking'
    : !isError && data?.status === 'ok'
      ? 'online'
      : 'offline'

  const dot = { checking: 'bg-faint', online: 'bg-available', offline: 'bg-error' }[state]
  const label = { checking: 'checking…', online: 'online', offline: 'offline' }[state]

  return (
    <span className="flex items-center gap-2 font-mono text-[11px] text-faint">
      <span aria-hidden className={cn('size-2 rounded-full', dot)} />
      {label}
    </span>
  )
}
