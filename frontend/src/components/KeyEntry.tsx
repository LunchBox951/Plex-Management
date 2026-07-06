import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { client } from '../api/client'
import { clearApiKey, disableApiKeyAuth, enableApiKeyAuth, setApiKey } from '../lib/apiKey'
import { Button } from './ui/Button'
import { Field } from './ui/Field'

/**
 * In-app recovery for an initialized install whose stored access key is missing
 * or invalid (new browser, cleared storage, rotated key). Without this the
 * operator would be bounced /setup -> / on every authenticated request with no
 * way to re-enter a key — i.e. recovery would require a terminal, which the
 * zero-terminal north star (ADR-0005) forbids.
 */
export function KeyEntry({
  onAuthenticated,
  onUsePlex,
}: {
  onAuthenticated: () => void
  onUsePlex?: () => void
}) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | undefined>(undefined)
  const [checking, setChecking] = useState(false)

  const submit = async () => {
    const key = value.trim()
    if (!key || checking) return
    setChecking(true)
    setError(undefined)
    setApiKey(key)
    enableApiKeyAuth()
    // Validate against a cheap protected endpoint before committing to it. The
    // GET can REJECT (not just return an error body) on a network/connection
    // failure — openapi-fetch rethrows when no onError middleware is registered —
    // so a try/finally keeps the screen honest and retryable instead of stranding
    // it on a stuck spinner.
    try {
      const { error: apiError } = await client.GET('/api/v1/settings')
      if (apiError) {
        clearApiKey()
        setError('That access key was rejected. Double-check it and try again.')
        return
      }
      await queryClient.invalidateQueries()
      onAuthenticated()
    } catch {
      clearApiKey()
      setError("Couldn't reach the server to check that key. Try again.")
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="mx-auto max-w-md px-5 py-24">
      <div className="rounded-2xl border border-hairline bg-surface p-6">
        <div className="font-display text-xl font-extrabold">Enter your access key</div>
        <p className="mt-2 text-sm text-muted">
          This install is already set up, but this browser doesn't have a valid access key. Paste the
          recovery key you generated from Settings → Access.
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
          <Button type="submit" loading={checking} disabled={value.trim().length === 0}>
            Continue
          </Button>
          {onUsePlex ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                disableApiKeyAuth()
                onUsePlex()
              }}
            >
              Use Plex sign-in
            </Button>
          ) : null}
        </form>
      </div>
    </div>
  )
}
