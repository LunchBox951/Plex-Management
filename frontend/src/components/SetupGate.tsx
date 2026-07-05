import { useEffect, useState } from 'react'
import { Navigate, Outlet, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { AUTH_EXPIRED_EVENT, AUTH_INVALID_EVENT, SETUP_REQUIRED_EVENT } from '../api/client'
import { useAuthMe, useSetupStatus } from '../api/hooks'
import { queryKeys } from '../lib/queryClient'
import { Button } from './ui/Button'
import { CenteredSpinner, StateMessage } from './ui/feedback'
import { KeyEntry } from './KeyEntry'
import { PlexLogin } from './PlexLogin'

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
    // An access-key 401: the stored key was rejected (the client already cleared
    // it). Show KeyEntry AND drop the cached "authenticated" answer from that key
    // — otherwise a stale ``authenticated: true`` from the prior successful key
    // login keeps rendering <Outlet/> (below) on protected screens that 401 on
    // every call. Refetching /auth/me (which never 401s — it returns
    // ``authenticated: false`` for a key-only session) re-derives honest state;
    // ``authMode === 'key'`` also takes precedence in the render so KeyEntry shows
    // immediately, before that refetch resolves.
    const onAuthInvalid = () => {
      setAuthMode('key')
      void queryClient.invalidateQueries({ queryKey: queryKeys.authMe })
    }
    // A session-cookie 401: the Plex sign-in lapsed. Drop the cached "authenticated"
    // answer and refetch /auth/me so the gate re-derives state — an expired session
    // resolves to `authenticated: false` and falls through to the Plex login, rather
    // than leaving stale authenticated UI stranded on error states with no way back.
    const onAuthExpired = () => {
      setAuthMode('plex')
      void queryClient.invalidateQueries({ queryKey: queryKeys.authMe })
    }
    window.addEventListener(SETUP_REQUIRED_EVENT, onSetupRequired)
    window.addEventListener(AUTH_INVALID_EVENT, onAuthInvalid)
    window.addEventListener(AUTH_EXPIRED_EVENT, onAuthExpired)
    return () => {
      window.removeEventListener(SETUP_REQUIRED_EVENT, onSetupRequired)
      window.removeEventListener(AUTH_INVALID_EVENT, onAuthInvalid)
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
    return <Outlet />
  }

  return <PlexLogin onUseAccessKey={() => setAuthMode('key')} />
}
