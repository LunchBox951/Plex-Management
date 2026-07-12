import { useState } from 'react'
import { usePlexSignIn } from '../api/hooks'
import { type ApiError, isApiError, toApiError } from '../lib/errors'
import { PlexPinError, openPlexPopup, runPlexPinFlow } from '../lib/plexOAuth'
import { AuthErrorCard } from './AuthErrorCard'
import { Button } from './ui/Button'

/**
 * Normalize a sign-in failure into what {@link AuthErrorCard} renders. Browser
 * PIN failures ({@link PlexPinError}) pass straight through; backend rejections
 * are already normalized `ApiError`s (thrown by `unwrap`) and pass through too;
 * any other throw is routed through {@link toApiError} so the card never
 * receives `undefined` and never renders a generic "went wrong".
 */
function toDisplayError(err: unknown): ApiError | PlexPinError {
  if (err instanceof PlexPinError) return err
  if (isApiError(err)) return err
  return toApiError(err)
}

/**
 * The Plex sign-in screen. Overseerr's popup + PIN dance: the popup is opened
 * SYNCHRONOUSLY from the click (before any `await`, or popup blockers null it),
 * then the browser polls plex.tv for the token and hands it to the backend to
 * verify. `onSignedIn` lets the caller (SetupGate or the setup wizard) decide
 * what happens once a session exists; `onUseAccessKey` is the OPTIONAL
 * recovery fallback for a browser without a working popup / Plex session — its
 * secondary button renders only when a handler is supplied. The setup wizard's
 * sign-in step omits it (a fresh pre-init install has no app key to recover);
 * SetupGate passes it so an operator locked out of Plex can fall back to a key.
 */
export function PlexLogin({
  onSignedIn,
  onUseAccessKey,
  embedded = false,
}: {
  onSignedIn: () => void
  onUseAccessKey?: () => void
  embedded?: boolean
}) {
  const signIn = usePlexSignIn()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | PlexPinError | undefined>(undefined)

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
      setError(toDisplayError(err))
    } finally {
      setBusy(false)
    }
  }

  const controls = (
    <>
      {embedded && error ? (
        <div className="mb-4 break-words">
          <AuthErrorCard error={error} />
        </div>
      ) : null}
      <div
        className={
          embedded
            ? 'flex flex-col gap-3 border-t border-hairline pt-5 sm:flex-row sm:justify-end'
            : 'flex flex-col gap-3'
        }
      >
        <Button
          className={embedded ? 'w-full sm:w-auto' : undefined}
          onClick={startSignIn}
          loading={busy}
        >
          Sign in with Plex
        </Button>
        {onUseAccessKey ? (
          <Button type="button" variant="secondary" onClick={onUseAccessKey}>
            Use access key
          </Button>
        ) : null}
      </div>
      {!embedded && error ? (
        <div className="mt-4">
          <AuthErrorCard error={error} />
        </div>
      ) : null}
    </>
  )

  if (embedded) return <div className="mt-6">{controls}</div>

  return (
    <div className="mx-auto max-w-md px-5 py-24">
      <div className="rounded-xl border border-hairline bg-surface p-6">
        <div className="font-display text-xl font-extrabold">Sign in</div>
        <p className="mt-2 text-sm text-muted">Use a Plex account with access to this server.</p>
        <div className="mt-5">{controls}</div>
      </div>
    </div>
  )
}
