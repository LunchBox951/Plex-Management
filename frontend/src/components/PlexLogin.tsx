import { useState } from 'react'
import { useStartPlexLogin } from '../api/hooks'
import { toApiError } from '../lib/errors'
import { rememberPlexLoginState } from '../lib/plexLoginState'
import { Button } from './ui/Button'
import { StateMessage } from './ui/feedback'

export function PlexLogin({ onUseAccessKey }: { onUseAccessKey: () => void }) {
  const start = useStartPlexLogin()
  const [error, setError] = useState<string | undefined>(undefined)

  const signIn = async () => {
    if (start.isPending) return
    setError(undefined)
    try {
      const result = await start.mutateAsync()
      rememberPlexLoginState(result.state)
      window.location.assign(result.auth_url)
    } catch (err) {
      setError(toApiError(err).message)
    }
  }

  return (
    <div className="mx-auto max-w-md px-5 py-24">
      <div className="rounded-xl border border-hairline bg-surface p-6">
        <div className="font-display text-xl font-extrabold">Sign in</div>
        <p className="mt-2 text-sm text-muted">Use a Plex account with access to this server.</p>
        <div className="mt-5 flex flex-col gap-3">
          <Button onClick={() => void signIn()} loading={start.isPending}>
            Sign in with Plex
          </Button>
          <Button type="button" variant="secondary" onClick={onUseAccessKey}>
            Use access key
          </Button>
        </div>
        {error ? (
          <div className="mt-4">
            <StateMessage tone="error" title="Sign-in failed" message={error} />
          </div>
        ) : null}
      </div>
    </div>
  )
}
