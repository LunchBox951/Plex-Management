import { useState } from 'react'
import { usePlexSignIn } from '../api/hooks'
import { type ApiError, toApiError } from '../lib/errors'
import { PlexPinError, openPlexPopup, runPlexPinFlow } from '../lib/plexOAuth'
import { Button } from './ui/Button'
import { StateMessage } from './ui/feedback'

/**
 * Turn a sign-in failure into an honest, retryable message. Browser-side PIN
 * failures ({@link PlexPinError}) aren't API errors, so humanize their typed
 * code the same way {@link toApiError} humanizes a backend `detail` — never a
 * generic "went wrong". Backend rejections are already normalized `ApiError`s
 * (thrown by `unwrap`) carrying a human message. Task 11's AuthErrorCard
 * replaces this with crafted per-code copy.
 */
function signInErrorMessage(err: unknown): string {
  if (err instanceof PlexPinError) {
    return toApiError({ detail: err.code }).message
  }
  return (err as ApiError).message
}

/**
 * The Plex sign-in screen. Overseerr's popup + PIN dance: the popup is opened
 * SYNCHRONOUSLY from the click (before any `await`, or popup blockers null it),
 * then the browser polls plex.tv for the token and hands it to the backend to
 * verify. `onSignedIn` lets the caller (SetupGate today; the wizard later)
 * decide what happens once a session exists; `onUseAccessKey` is the recovery
 * fallback for a browser without a working popup / Plex session.
 */
export function PlexLogin({
  onSignedIn,
  onUseAccessKey,
}: {
  onSignedIn: () => void
  onUseAccessKey: () => void
}) {
  const signIn = usePlexSignIn()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | undefined>(undefined)

  const startSignIn = () => {
    if (busy) return
    setError(undefined)
    setBusy(true)
    // Pre-open the popup in the click's call stack — MUST be before any await.
    const popup = openPlexPopup()
    void runSignIn(popup)
  }

  const runSignIn = async (popup: Window | null) => {
    try {
      const authToken = await runPlexPinFlow(popup)
      await signIn.mutateAsync({ auth_token: authToken })
      onSignedIn()
    } catch (err) {
      setError(signInErrorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-md px-5 py-24">
      <div className="rounded-xl border border-hairline bg-surface p-6">
        <div className="font-display text-xl font-extrabold">Sign in</div>
        <p className="mt-2 text-sm text-muted">Use a Plex account with access to this server.</p>
        <div className="mt-5 flex flex-col gap-3">
          <Button onClick={startSignIn} loading={busy}>
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
