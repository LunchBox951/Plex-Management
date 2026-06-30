import { useEffect, useState } from 'react'
import { useSettings, useUpdateSettings } from '../api/hooks'
import type { SettingsResponse, SettingsUpdate } from '../api/types'
import type { ApiError } from '../lib/errors'
import { Button } from '../components/ui/Button'
import { LinkButton } from '../components/ui/LinkButton'
import { Field } from '../components/ui/Field'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'

/** A secret value is rendered by the backend as this sentinel when configured. */
const SECRET_SET = '***'

interface FormState {
  plex_url: string
  plex_token: string
  prowlarr_url: string
  prowlarr_api_key: string
  qbittorrent_url: string
  qbittorrent_username: string
  qbittorrent_password: string
  tmdb_api_key: string
}

/** Plaintext fields prefill from current values; secret inputs always start empty. */
function initialForm(data: SettingsResponse): FormState {
  return {
    plex_url: data.plex_url ?? '',
    plex_token: '',
    prowlarr_url: data.prowlarr_url ?? '',
    prowlarr_api_key: '',
    qbittorrent_url: data.qbittorrent_url ?? '',
    qbittorrent_username: data.qbittorrent_username ?? '',
    qbittorrent_password: '',
    tmdb_api_key: '',
  }
}

type TextKey = 'plex_url' | 'prowlarr_url' | 'qbittorrent_url' | 'qbittorrent_username'
type SecretKey = 'plex_token' | 'prowlarr_api_key' | 'qbittorrent_password' | 'tmdb_api_key'

const Heading = () => <h1 className="font-display text-2xl font-extrabold">Settings</h1>

export function Settings() {
  const { data, isLoading, isError, error, refetch } = useSettings()
  const update = useUpdateSettings()
  const { toast } = useToast()

  // Controlled state, seeded once the settings have loaded.
  const [form, setForm] = useState<FormState | null>(null)
  useEffect(() => {
    if (data && form === null) setForm(initialForm(data))
  }, [data, form])

  if (isLoading || (data && form === null)) {
    return (
      <div className="flex flex-col gap-6">
        <Heading />
        <CenteredSpinner label="Loading settings…" />
      </div>
    )
  }

  if (isError || !data || !form) {
    return (
      <div className="flex flex-col gap-6">
        <Heading />
        <StateMessage
          tone="error"
          title="Couldn't load settings"
          message={error?.message ?? 'Settings are unavailable right now.'}
          action={
            <Button variant="secondary" onClick={() => void refetch()}>
              Retry
            </Button>
          }
        />
      </div>
    )
  }

  const setField = (key: keyof FormState, value: string) =>
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev))

  const textField = (key: TextKey, label: string, placeholder: string) => (
    <Field
      label={label}
      value={form[key]}
      onChange={(e) => setField(key, e.target.value)}
      placeholder={placeholder}
    />
  )

  const secretField = (key: SecretKey, label: string, isSet: boolean) => (
    <Field
      label={label}
      type="password"
      autoComplete="off"
      value={form[key]}
      onChange={(e) => setField(key, e.target.value)}
      placeholder={isSet ? '•••• set' : 'Not set'}
      hint={isSet ? '•••• set (leave blank to keep)' : undefined}
    />
  )

  const handleSave = async () => {
    // Plaintext fields always written; secrets only when the user typed a value,
    // so an untouched secret stays the backend's no-op (left unchanged).
    const body: SettingsUpdate = {
      plex_url: form.plex_url,
      prowlarr_url: form.prowlarr_url,
      qbittorrent_url: form.qbittorrent_url,
      qbittorrent_username: form.qbittorrent_username,
    }
    if (form.plex_token) body.plex_token = form.plex_token
    if (form.prowlarr_api_key) body.prowlarr_api_key = form.prowlarr_api_key
    if (form.qbittorrent_password) body.qbittorrent_password = form.qbittorrent_password
    if (form.tmdb_api_key) body.tmdb_api_key = form.tmdb_api_key

    try {
      await update.mutateAsync(body)
      toast({ title: 'Settings saved', intent: 'success' })
      // Clear secret inputs so they reflect the now-masked stored values.
      setForm((prev) =>
        prev
          ? {
              ...prev,
              plex_token: '',
              prowlarr_api_key: '',
              qbittorrent_password: '',
              tmdb_api_key: '',
            }
          : prev,
      )
    } catch (err) {
      const apiError = err as ApiError
      toast({ title: 'Save failed', description: apiError.message, intent: 'error' })
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Heading />

      <div className="flex flex-col gap-5">
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Plex</h2>
          <div className="mt-4 flex flex-col gap-4">
            {textField('plex_url', 'URL', 'http://localhost:32400')}
            {secretField('plex_token', 'Token', data.plex_token === SECRET_SET)}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Prowlarr</h2>
          <div className="mt-4 flex flex-col gap-4">
            {textField('prowlarr_url', 'URL', 'http://localhost:9696')}
            {secretField('prowlarr_api_key', 'API key', data.prowlarr_api_key === SECRET_SET)}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">qBittorrent</h2>
          <div className="mt-4 flex flex-col gap-4">
            {textField('qbittorrent_url', 'URL', 'http://localhost:8080')}
            {textField('qbittorrent_username', 'Username', 'admin')}
            {secretField('qbittorrent_password', 'Password', data.qbittorrent_password === SECRET_SET)}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">TMDB</h2>
          <div className="mt-4 flex flex-col gap-4">
            {secretField('tmdb_api_key', 'API key', data.tmdb_api_key === SECRET_SET)}
          </div>
        </section>
      </div>

      <div>
        <Button loading={update.isPending} onClick={() => void handleSave()}>
          Save changes
        </Button>
      </div>

      <section className="rounded-xl border border-hairline bg-surface p-5">
        <h2 className="font-display text-sm font-semibold text-ink">More</h2>
        <div className="mt-4 flex flex-wrap gap-3">
          <LinkButton variant="secondary" to="/blocklist">
            Manage blocklist
          </LinkButton>
          <LinkButton variant="secondary" to="/quality">
            View quality profile
          </LinkButton>
        </div>
      </section>
    </div>
  )
}
