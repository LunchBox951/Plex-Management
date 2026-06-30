import { useEffect, useState } from 'react'
import { Navigate, Outlet, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { AUTH_INVALID_EVENT, SETUP_REQUIRED_EVENT } from '../api/client'
import { useSetupStatus } from '../api/hooks'
import { queryKeys } from '../lib/queryClient'
import { Button } from './ui/Button'
import { CenteredSpinner, StateMessage } from './ui/feedback'
import { KeyEntry } from './KeyEntry'

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
  const [authFailed, setAuthFailed] = useState(false)

  useEffect(() => {
    const onSetupRequired = () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.setupStatus })
      navigate('/setup', { replace: true })
    }
    const onAuthInvalid = () => setAuthFailed(true)
    window.addEventListener(SETUP_REQUIRED_EVENT, onSetupRequired)
    window.addEventListener(AUTH_INVALID_EVENT, onAuthInvalid)
    return () => {
      window.removeEventListener(SETUP_REQUIRED_EVENT, onSetupRequired)
      window.removeEventListener(AUTH_INVALID_EVENT, onAuthInvalid)
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

  if (authFailed) {
    return <KeyEntry onAuthenticated={() => setAuthFailed(false)} />
  }

  return <Outlet />
}
