import { useQuery } from '@tanstack/react-query'
import { client } from '../api/client'
import { Dot, type DotTone } from './ui/Dot'

const TONE: Record<'checking' | 'online' | 'offline', DotTone> = {
  checking: 'neutral',
  online: 'ok',
  offline: 'error',
}
const LABEL: Record<'checking' | 'online' | 'offline', string> = {
  checking: 'checking…',
  online: 'online',
  offline: 'offline',
}

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

  return <Dot tone={TONE[state]} label={LABEL[state]} />
}
