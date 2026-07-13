import { useState } from 'react'
import { useExchangeApiKey } from '../api/hooks'
import { isApiError } from '../lib/errors'
import { Button } from './ui/Button'
import { Field } from './ui/Field'

/**
 * In-app break-glass recovery for an initialized install: the operator pastes the
 * recovery key (Settings → Access) when Plex sign-in is unavailable (plex.tv
 * outage, or a browser with no Plex account). Without this the operator would be
 * bounced on every authenticated request with no terminal-free way back in, which
 * the zero-terminal north star (ADR-0005) forbids.
 *
 * The key is exchanged ONCE for the same HTTP-only session cookie the Plex flow
 * issues (`POST /api/v1/auth/api-key`): it rides a single request header and is
 * never stored in the browser, so nothing JS-readable ever holds it (CodeQL
 * #263). Recovery thereafter runs on the cookie exactly like a Plex session.
 */
export function KeyEntry({
  onAuthenticated,
  onUsePlex,
}: {
  onAuthenticated: () => void
  onUsePlex?: () => void
}) {
  const exchange = useExchangeApiKey()
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | undefined>(undefined)

  const submit = async () => {
    const key = value.trim()
    if (!key || exchange.isPending) return
    setError(undefined)
    try {
      await exchange.mutateAsync(key)
      onAuthenticated()
    } catch (err) {
      // Keep the two failure modes distinct so the operator knows what to fix
      // (north star #3): a 401 is a wrong key to re-check; anything else (network
      // drop, 5xx, throttle) is a reach-the-server problem, not a bad secret.
      // Either way the screen stays honest and retryable rather than proceeding.
      setError(
        isApiError(err) && err.status === 401
          ? 'That access key was rejected. Double-check it and try again.'
          : "Couldn't reach the server to verify that key. Try again in a moment.",
      )
    }
  }

  return (
    <div className="mx-auto max-w-md px-5 py-24">
      <div className="rounded-2xl border border-hairline bg-surface p-6">
        <div className="font-display text-xl font-extrabold">Enter your access key</div>
        <p className="mt-2 text-sm text-muted">
          This install is already set up, but this browser isn't signed in. Paste the recovery key
          you generated from Settings → Access.
        </p>
        <form
          className="mt-5 flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <Field
            label="Access key"
            type="password"
            autoComplete="off"
            autoFocus
            value={value}
            onChange={(e) => setValue(e.target.value)}
            error={error}
          />
          <Button type="submit" loading={exchange.isPending} disabled={value.trim().length === 0}>
            Continue
          </Button>
          {onUsePlex ? (
            <Button type="button" variant="secondary" onClick={onUsePlex}>
              Use Plex sign-in
            </Button>
          ) : null}
        </form>
      </div>
    </div>
  )
}
