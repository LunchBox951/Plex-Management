import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useCompletePlexLogin } from '../api/hooks'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { toApiError } from '../lib/errors'
import {
  clearRememberedPlexLoginState,
  readRememberedPlexLoginState,
} from '../lib/plexLoginState'

export function PlexCallback() {
  const location = useLocation()
  const navigate = useNavigate()
  const { mutateAsync: completeLogin } = useCompletePlexLogin()
  const [error, setError] = useState<string | undefined>(undefined)

  useEffect(() => {
    const state = new URLSearchParams(location.search).get('state') ?? readRememberedPlexLoginState()
    if (!state) {
      setError("Couldn't complete Plex sign-in.")
      return
    }
    let cancelled = false
    completeLogin({ state })
      .then(() => {
        if (cancelled) return
        clearRememberedPlexLoginState()
        navigate('/', { replace: true })
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(toApiError(err).message)
      })
    return () => {
      cancelled = true
    }
  }, [completeLogin, location.search, navigate])

  if (error) {
    return (
      <div className="mx-auto max-w-md px-5 py-24">
        <StateMessage tone="error" title="Couldn't complete Plex sign-in" message={error} />
      </div>
    )
  }

  return <CenteredSpinner label="Completing sign-in…" />
}
