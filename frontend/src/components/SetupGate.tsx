import { useEffect, useState } from 'react'
import { Navigate, Outlet, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { AUTH_EXPIRED_EVENT, SETUP_REQUIRED_EVENT } from '../api/client'
import { useAuthMe, useSetupStatus } from '../api/hooks'
import { queryKeys } from '../lib/queryClient'
import { Button } from './ui/Button'
import { CenteredSpinner, StateMessage } from './ui/feedback'
import { KeyEntry } from './KeyEntry'
import { PlexLogin } from './PlexLogin'
import { RealtimeProvider } from './RealtimeProvider'

/**
 * Gate for every authenticated screen. Reads install state and routes:
 *   - not initialized          -> the setup wizard;
 *   - 409 setup_required event  -> the wizard (backend says it's not set up yet);
 *   - 401 invalid-key event     -> the in-app KeyEntry recovery screen, NOT a
 *     bounce to the wizard (which would self-redirect to "/" on an initialized
 *     install and strand the operator in a loop).
 */
export function SetupGate() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, refetch } = useSetupStatus()
  const auth = useAuthMe(data?.initialized === true)
  const [authMode, setAuthMode] = useState<'plex' | 'key'>('plex')

  useEffect(() => {
    const onSetupRequired = () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.setupStatus })
      navigate('/setup', { replace: true })
    }
    // A session-cookie 401: the browser session (Plex sign-in OR a recovery-key
    // exchange) lapsed. Drop the cached "authenticated" answer and refetch
    // /auth/me so the gate re-derives state — an expired session resolves to
    // `authenticated: false` and falls through to the Plex login (whose "Use
    // access key" affordance re-opens the break-glass KeyEntry), rather than
    // leaving stale authenticated UI stranded on error states with no way back.
    const onAuthExpired = () => {
      setAuthMode('plex')
      void queryClient.invalidateQueries({ queryKey: queryKeys.authMe })
    }
    window.addEventListener(SETUP_REQUIRED_EVENT, onSetupRequired)
    window.addEventListener(AUTH_EXPIRED_EVENT, onAuthExpired)
    return () => {
      window.removeEventListener(SETUP_REQUIRED_EVENT, onSetupRequired)
      window.removeEventListener(AUTH_EXPIRED_EVENT, onAuthExpired)
    }
  }, [navigate, queryClient])

  if (isLoading) return <CenteredSpinner label="Loading…" />

  if (isError) {
    return (
      <div className="mx-auto max-w-md px-5 py-24">
        <StateMessage
          tone="error"
          title="Can't reach the server"
          message="The Plex Manager API didn't respond. Check that the service is running, then retry."
          action={
            <Button onClick={() => void refetch()} variant="secondary">
              Retry
            </Button>
          }
        />
      </div>
    )
  }

  if (!data?.initialized) {
    return <Navigate to="/setup" replace />
  }

  if (auth.isLoading) return <CenteredSpinner label="Checking sign-in…" />

  // KeyEntry takes precedence over a cached ``authenticated: true``: after an
  // access-key 401 the stored key is gone, so that cached answer is stale and
  // must not strand the operator on <Outlet/> (see ``onAuthInvalid``).
  if (authMode === 'key') {
    return (
      <KeyEntry
        onAuthenticated={() => {
          setAuthMode('plex')
          void auth.refetch()
        }}
        onUsePlex={() => setAuthMode('plex')}
      />
    )
  }

  if (auth.data?.authenticated) {
    if (!auth.data.is_admin) {
      return <Outlet />
    }
    return (
      <RealtimeProvider>
        <Outlet />
      </RealtimeProvider>
    )
  }

  // usePlexSignIn already seeds /auth/me and invalidates queries on success; the
  // refetch here re-derives gate state so the newly-authenticated session lands
  // on <Outlet/> without a reload. onUseAccessKey drops to the KeyEntry recovery
  // path for a browser locked out of Plex.
  return (
    <PlexLogin onSignedIn={() => void auth.refetch()} onUseAccessKey={() => setAuthMode('key')} />
  )
}
