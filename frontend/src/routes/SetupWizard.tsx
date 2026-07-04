import { useRef, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import {
  type SetupService,
  useCompleteSetup,
  useSetupStatus,
  useValidateService,
} from '../api/hooks'
import type { PlexLibraryOption, SetupCompleteRequest } from '../api/types'
import { clearSetupToken, setApiKey, setSetupToken } from '../lib/apiKey'
import type { ApiError } from '../lib/errors'
import { cn } from '../lib/cn'
import { Button } from '../components/ui/Button'
import { Field } from '../components/ui/Field'
import { CenteredSpinner } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'

// `movies_root` and `tv_root` are each optional/nullable (unlike every other
// field here, which is a required string) and get their own dedicated Library
// sections below rather than the generic field loop, so they are excluded here
// — keeps `form[field.key]` a plain `string` for every key this type actually
// carries. `anime_movie_root`/`anime_tv_root` (ADR-0015) get the SAME
// treatment — optional, their own section, excluded from the generic loop.
type FormKey = Exclude<
  keyof SetupCompleteRequest,
  'movies_root' | 'tv_root' | 'anime_movie_root' | 'anime_tv_root'
>

interface FieldDef {
  key: FormKey
  label: string
  type: 'text' | 'password'
  placeholder?: string
}

interface ServiceDef {
  key: SetupService
  label: string
  blurb: string
  fields: FieldDef[]
}

const SERVICES: ServiceDef[] = [
  {
    key: 'plex',
    label: 'Plex',
    blurb: 'Your media server — where requested titles become watchable.',
    fields: [
      { key: 'plex_url', label: 'Server URL', type: 'text', placeholder: 'http://localhost:32400' },
      { key: 'plex_token', label: 'Plex token', type: 'password' },
    ],
  },
  {
    key: 'prowlarr',
    label: 'Prowlarr',
    blurb: 'The indexer manager searched for release candidates.',
    fields: [
      { key: 'prowlarr_url', label: 'URL', type: 'text', placeholder: 'http://localhost:9696' },
      { key: 'prowlarr_api_key', label: 'API key', type: 'password' },
    ],
  },
  {
    key: 'qbittorrent',
    label: 'qBittorrent',
    blurb: 'The download client that fetches the chosen release.',
    fields: [
      { key: 'qbittorrent_url', label: 'URL', type: 'text', placeholder: 'http://localhost:8080' },
      { key: 'qbittorrent_username', label: 'Username', type: 'text', placeholder: 'admin' },
      { key: 'qbittorrent_password', label: 'Password', type: 'password' },
    ],
  },
  {
    key: 'tmdb',
    label: 'TMDB',
    blurb: 'The metadata source powering Discover search.',
    fields: [{ key: 'tmdb_api_key', label: 'API key', type: 'password' }],
  },
]

const EMPTY_FORM: SetupCompleteRequest = {
  plex_url: '',
  plex_token: '',
  prowlarr_url: '',
  prowlarr_api_key: '',
  qbittorrent_url: '',
  qbittorrent_username: '',
  qbittorrent_password: '',
  tmdb_api_key: '',
  movies_root: '',
  tv_root: '',
  anime_movie_root: '',
  anime_tv_root: '',
}

const EMPTY_TESTING: Record<SetupService, boolean> = {
  plex: false,
  prowlarr: false,
  qbittorrent: false,
  tmdb: false,
}

interface TestResult {
  ok: boolean
  message: string
}

function asApiError(error: unknown): ApiError {
  return error as ApiError
}

function bodyFor(service: SetupService, form: SetupCompleteRequest): Record<string, string> {
  switch (service) {
    case 'plex':
      return { url: form.plex_url, token: form.plex_token }
    case 'prowlarr':
      return { url: form.prowlarr_url, api_key: form.prowlarr_api_key }
    case 'qbittorrent':
      return {
        url: form.qbittorrent_url,
        username: form.qbittorrent_username,
        password: form.qbittorrent_password,
      }
    case 'tmdb':
      return { api_key: form.tmdb_api_key }
  }
}

export function SetupWizard() {
  const navigate = useNavigate()
  const { toast } = useToast()
  const status = useSetupStatus()
  const validate = useValidateService()
  const complete = useCompleteSetup()

  const [form, setForm] = useState<SetupCompleteRequest>(EMPTY_FORM)
  const [results, setResults] = useState<Record<SetupService, TestResult | null>>({
    plex: null,
    prowlarr: null,
    qbittorrent: null,
    tmdb: null,
  })
  const [testing, setTesting] = useState<Record<SetupService, boolean>>(EMPTY_TESTING)
  const [mintedKey, setMintedKey] = useState<string | null>(null)
  const [setupTokenInput, setSetupTokenInput] = useState('')
  // Movie AND tv library folders Plex reports (set when Plex verifies), each
  // tagged by `section_type` — filtered per-picker below. `null` until then.
  const [plexLibraries, setPlexLibraries] = useState<PlexLibraryOption[] | null>(null)
  // Reveal a typed override instead of the Plex pick-list (split-mount / odd layout).
  const [manualPath, setManualPath] = useState(false)
  // Same, for the (optional) tv library folder.
  const [manualTvPath, setManualTvPath] = useState(false)
  // Same, for the (optional) anime library folders (ADR-0015).
  const [manualAnimeMoviePath, setManualAnimeMoviePath] = useState(false)
  const [manualAnimeTvPath, setManualAnimeTvPath] = useState(false)
  // Per-service generation: bumped on every edit so an in-flight validation whose
  // fields changed underneath it is discarded (never marks stale creds verified).
  const validationGen = useRef<Record<SetupService, number>>({
    plex: 0,
    prowlarr: 0,
    qbittorrent: 0,
    tmdb: 0,
  })

  const copyKey = async () => {
    if (!mintedKey) return
    try {
      await navigator.clipboard.writeText(mintedKey)
      toast({ title: 'Copied to clipboard', intent: 'success' })
    } catch {
      toast({ title: 'Copy failed', description: 'Select the key and copy it manually.', intent: 'error' })
    }
  }

  if (status.isLoading) return <CenteredSpinner label="Loading…" />

  // Setup just completed: reveal the one-time key BEFORE leaving (it is shown
  // exactly once and is the only thing the in-app key-recovery screen can accept).
  if (mintedKey) {
    return (
      <div className="mx-auto max-w-md px-5 py-24">
        <div className="rounded-2xl border border-available/40 bg-surface p-6">
          <div className="font-display text-xl font-extrabold text-available">✓ Setup complete</div>
          <p className="mt-2 text-sm text-muted">
            Save your <span className="text-ink">access key</span>. It is shown{' '}
            <span className="text-ink">only once</span> — you'll need it to sign in from another
            browser, or if this browser's storage is cleared.
          </p>
          <div className="mt-4 flex items-center gap-2 rounded-lg bg-bg p-3 ring-1 ring-inset ring-white/10">
            <code className="min-w-0 flex-1 truncate font-mono text-sm text-gold select-all">
              {mintedKey}
            </code>
            <Button variant="secondary" size="sm" onClick={() => void copyKey()}>
              Copy
            </Button>
          </div>
          <Button className="mt-6 w-full" onClick={() => navigate('/', { replace: true })}>
            I've saved it — continue
          </Button>
        </div>
      </div>
    )
  }

  // Already configured -> the wizard has nothing to do.
  if (status.data?.initialized) return <Navigate to="/" replace />

  const setField = (key: FormKey, value: string, service: SetupService) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    // Editing a field invalidates that service's prior test result + any in-flight one.
    setResults((prev) => ({ ...prev, [service]: null }))
    validationGen.current[service] += 1
    // Editing Plex creds invalidates the library pick-lists + any chosen folders
    // (they belonged to the old server).
    if (service === 'plex') {
      setPlexLibraries(null)
      setManualPath(false)
      setManualTvPath(false)
      setManualAnimeMoviePath(false)
      setManualAnimeTvPath(false)
      setForm((prev) => ({
        ...prev,
        movies_root: '',
        tv_root: '',
        anime_movie_root: '',
        anime_tv_root: '',
      }))
    }
  }

  const test = async (service: SetupService) => {
    const gen = validationGen.current[service]
    setTesting((prev) => ({ ...prev, [service]: true }))
    try {
      const res = await validate.mutateAsync({ service, body: bodyFor(service, form) })
      if (validationGen.current[service] !== gen) return // fields changed; ignore stale result
      setResults((prev) => ({ ...prev, [service]: { ok: res.ok, message: res.message } }))
      // Plex returns its movie AND tv library folders — drive both pick-lists.
      if (service === 'plex' && res.ok) setPlexLibraries(res.libraries ?? [])
    } catch (error) {
      if (validationGen.current[service] !== gen) return
      setResults((prev) => ({
        ...prev,
        [service]: { ok: false, message: asApiError(error).message },
      }))
    } finally {
      setTesting((prev) => ({ ...prev, [service]: false }))
    }
  }

  const servicesVerified = SERVICES.every((s) => results[s.key]?.ok === true)
  const verifiedCount = SERVICES.filter((s) => results[s.key]?.ok === true).length
  const plexVerified = results.plex?.ok === true
  // `plexLibraries` carries BOTH movie and tv folders, each tagged by
  // `section_type` — split per-picker below.
  const movieLibraries = plexLibraries?.filter((l) => l.section_type === 'movie') ?? null
  const tvLibraries = plexLibraries?.filter((l) => l.section_type === 'tv') ?? null
  const setupTokenReady =
    status.data?.setup_token_required !== true || setupTokenInput.trim().length > 0
  // Completion needs at least ONE library root — movies, tv, OR either anime root
  // (ADR-0015). Every root is independently optional (ADR-0011: a movie-only OR a
  // tv-only Plex is legit; the backend normalizes an empty root to None and surfaces
  // a per-type ImportBlocked only for the missing kind). Forcing movies_root would
  // lock a tv-only operator out of setup entirely — they have no movie library to
  // point at. An anime-ONLY install (backend-supported: anime imports route to their
  // own roots) must likewise be completable off an anime root alone, so the anime
  // roots count toward the gate too.
  const hasLibraryRoot =
    (form.movies_root ?? '').trim() !== '' ||
    (form.tv_root ?? '').trim() !== '' ||
    (form.anime_movie_root ?? '').trim() !== '' ||
    (form.anime_tv_root ?? '').trim() !== ''
  const allVerified = servicesVerified && setupTokenReady && hasLibraryRoot

  const onComplete = async () => {
    try {
      if (status.data?.setup_token_required) {
        setSetupToken(setupTokenInput.trim())
      }
      const res = await complete.mutateAsync(form)
      if (res.app_api_key) {
        setApiKey(res.app_api_key)
        clearSetupToken()
        // Reveal it once before navigating (see the mintedKey branch above).
        setMintedKey(res.app_api_key)
      } else {
        // No key returned (already-initialized edge) — just proceed.
        navigate('/', { replace: true })
      }
    } catch (error) {
      toast({ title: 'Setup failed', description: asApiError(error).message, intent: 'error' })
    }
  }

  return (
    <div className="mx-auto max-w-2xl px-5 py-12">
      <header className="mb-8 text-center">
        <div className="font-display text-2xl font-extrabold tracking-wide">
          PLEX<span className="text-gold">MGR</span>
        </div>
        <h1 className="mt-4 font-display text-3xl font-extrabold">Welcome — let's connect things</h1>
        <p className="mt-2 text-muted">
          Enter and test each service. Credentials are stored encrypted; you never touch a terminal.
        </p>
      </header>

      {status.data?.setup_token_required ? (
        <section className="mb-4 rounded-2xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-lg font-bold text-ink">Setup token</h2>
          <p className="mt-1 text-sm text-muted">
            Enter the one-time bootstrap token from your server's environment.
          </p>
          <div className="mt-4">
            <Field
              label="Setup token"
              type="password"
              autoComplete="off"
              value={setupTokenInput}
              onChange={(e) => {
                const value = e.target.value
                setSetupTokenInput(value)
                if (value.trim()) {
                  setSetupToken(value.trim())
                } else {
                  clearSetupToken()
                }
              }}
            />
          </div>
        </section>
      ) : null}

      <div className="flex flex-col gap-4">
        {SERVICES.map((service) => {
          const result = results[service.key]
          return (
            <section
              key={service.key}
              className={cn(
                'rounded-2xl border bg-surface p-5 transition-colors',
                result?.ok ? 'border-available/40' : 'border-hairline',
              )}
            >
              <div className="flex items-baseline justify-between gap-3">
                <h2 className="font-display text-lg font-bold text-ink">{service.label}</h2>
                {result?.ok ? (
                  <span className="font-mono text-xs text-available">✓ verified</span>
                ) : null}
              </div>
              <p className="mt-1 text-sm text-muted">{service.blurb}</p>

              <div className="mt-4 flex flex-col gap-4">
                {service.fields.map((field) => (
                  <Field
                    key={field.key}
                    label={field.label}
                    type={field.type}
                    {...(field.type === 'password' ? { autoComplete: 'off' } : {})}
                    {...(field.placeholder ? { placeholder: field.placeholder } : {})}
                    value={form[field.key]}
                    onChange={(e) => setField(field.key, e.target.value, service.key)}
                  />
                ))}
              </div>

              <div className="mt-4 flex items-center gap-3">
                <Button
                  variant="secondary"
                  size="sm"
                  loading={testing[service.key]}
                  disabled={!setupTokenReady}
                  onClick={() => void test(service.key)}
                >
                  Test connection
                </Button>
                {result && !result.ok ? (
                  <span className="text-sm text-error">{result.message}</span>
                ) : result?.ok ? (
                  <span className="text-sm text-available">{result.message}</span>
                ) : null}
              </div>
            </section>
          )
        })}
      </div>

      <section
        className={cn(
          'mt-4 rounded-2xl border bg-surface p-5 transition-colors',
          form.movies_root ? 'border-available/40' : 'border-hairline',
        )}
      >
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-display text-lg font-bold text-ink">Library</h2>
          {form.movies_root ? (
            <span className="font-mono text-xs text-available">✓ chosen</span>
          ) : null}
        </div>
        <p className="mt-1 text-sm text-muted">
          Where imported movies are placed — pick a folder Plex already watches.
        </p>
        {!plexVerified ? (
          <p className="mt-4 text-sm text-faint">
            Verify Plex above to choose your movie library folder.
          </p>
        ) : !manualPath && movieLibraries && movieLibraries.length > 0 ? (
          <div className="mt-4 flex flex-col gap-2">
            <select
              aria-label="Movies library folder"
              className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
              value={form.movies_root ?? ''}
              onChange={(e) => setForm((prev) => ({ ...prev, movies_root: e.target.value }))}
            >
              <option value="">Choose a movie library folder…</option>
              {movieLibraries.map((lib) => (
                <option
                  key={`${lib.section_key}:${lib.path}`}
                  value={lib.path}
                  disabled={lib.writable === false}
                >
                  {lib.title} — {lib.path}
                  {lib.writable === false ? ' · not writable by the app' : ''}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="self-start text-xs text-gold hover:underline"
              onClick={() => setManualPath(true)}
            >
              Use a custom path instead
            </button>
          </div>
        ) : (
          <div className="mt-4 flex flex-col gap-2">
            <Field
              label="Movies library folder"
              type="text"
              placeholder="/library/movies"
              value={form.movies_root ?? ''}
              onChange={(e) => setForm((prev) => ({ ...prev, movies_root: e.target.value }))}
            />
            {movieLibraries && movieLibraries.length > 0 ? (
              <button
                type="button"
                className="self-start text-xs text-gold hover:underline"
                onClick={() => setManualPath(false)}
              >
                ← Pick from a Plex library instead
              </button>
            ) : (
              <p className="text-xs text-faint">
                Plex reports no movie library — leave this unset if you don't request movies, or enter
                a writable folder the app places movies into.
              </p>
            )}
          </div>
        )}
      </section>

      {/* TV is entirely optional (ADR-0011): a movie-only install skips this
          section with no consequence — `tv_root` never gates `allVerified`. */}
      <section className="mt-4 rounded-2xl border border-hairline bg-surface p-5 transition-colors">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-display text-lg font-bold text-ink">TV Library</h2>
          {form.tv_root ? (
            <span className="font-mono text-xs text-available">✓ chosen</span>
          ) : (
            <span className="font-mono text-xs text-faint">optional</span>
          )}
        </div>
        <p className="mt-1 text-sm text-muted">
          Where imported tv seasons are placed — pick a folder Plex already watches. Leave
          unset if you don't request tv shows.
        </p>
        {!plexVerified ? (
          <p className="mt-4 text-sm text-faint">Verify Plex above to choose your tv library folder.</p>
        ) : !manualTvPath && tvLibraries && tvLibraries.length > 0 ? (
          <div className="mt-4 flex flex-col gap-2">
            <select
              aria-label="TV library folder"
              className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
              value={form.tv_root ?? ''}
              onChange={(e) => setForm((prev) => ({ ...prev, tv_root: e.target.value }))}
            >
              <option value="">Skip TV for now…</option>
              {tvLibraries.map((lib) => (
                <option
                  key={`${lib.section_key}:${lib.path}`}
                  value={lib.path}
                  disabled={lib.writable === false}
                >
                  {lib.title} — {lib.path}
                  {lib.writable === false ? ' · not writable by the app' : ''}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="self-start text-xs text-gold hover:underline"
              onClick={() => setManualTvPath(true)}
            >
              Use a custom path instead
            </button>
          </div>
        ) : (
          <div className="mt-4 flex flex-col gap-2">
            <Field
              label="TV library folder"
              type="text"
              placeholder="/library/tv"
              value={form.tv_root ?? ''}
              onChange={(e) => setForm((prev) => ({ ...prev, tv_root: e.target.value }))}
            />
            {tvLibraries && tvLibraries.length > 0 ? (
              <button
                type="button"
                className="self-start text-xs text-gold hover:underline"
                onClick={() => setManualTvPath(false)}
              >
                ← Pick from a Plex library instead
              </button>
            ) : (
              <p className="text-xs text-faint">
                Plex reports no tv library — enter the folder the app writes tv into (it must be
                writable), or leave blank to skip TV.
              </p>
            )}
          </div>
        )}
      </section>

      {/* Anime library routing (ADR-0015) — both entirely OPTIONAL and never
          gate `allVerified`/`hasLibraryRoot`: unset anime roots simply route
          anime imports to the Movies/TV roots above, identical to behavior
          before this feature existed. Reuses the SAME Plex library lists
          (an anime library is an ordinary Plex movie/tv section). */}
      <section className="mt-4 rounded-2xl border border-hairline bg-surface p-5 transition-colors">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-display text-lg font-bold text-ink">Anime library</h2>
          <span className="font-mono text-xs text-faint">optional</span>
        </div>
        <p className="mt-1 text-sm text-muted">
          Route anime movies/episodes to a separate Plex library instead of the Movies/TV
          folders above. Leave unset to keep anime in the normal libraries.
        </p>
        {!plexVerified ? (
          <p className="mt-4 text-sm text-faint">
            Verify Plex above to choose your anime library folders.
          </p>
        ) : (
          <div className="mt-4 flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              {!manualAnimeMoviePath && movieLibraries && movieLibraries.length > 0 ? (
                <>
                  <select
                    aria-label="Anime movies library folder"
                    className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                    value={form.anime_movie_root ?? ''}
                    onChange={(e) => setForm((prev) => ({ ...prev, anime_movie_root: e.target.value }))}
                  >
                    <option value="">No anime movies library folder…</option>
                    {movieLibraries.map((lib) => (
                      <option
                        key={`${lib.section_key}:${lib.path}`}
                        value={lib.path}
                        disabled={lib.writable === false}
                      >
                        {lib.title} — {lib.path}
                        {lib.writable === false ? ' · not writable by the app' : ''}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="self-start text-xs text-gold hover:underline"
                    onClick={() => setManualAnimeMoviePath(true)}
                  >
                    Use a custom path instead
                  </button>
                </>
              ) : (
                <div className="flex flex-col gap-2">
                  <Field
                    label="Anime movies library folder"
                    type="text"
                    placeholder="/library/anime-movies"
                    value={form.anime_movie_root ?? ''}
                    onChange={(e) => setForm((prev) => ({ ...prev, anime_movie_root: e.target.value }))}
                  />
                  {movieLibraries && movieLibraries.length > 0 ? (
                    <button
                      type="button"
                      className="self-start text-xs text-gold hover:underline"
                      onClick={() => setManualAnimeMoviePath(false)}
                    >
                      ← Pick from a Plex library instead
                    </button>
                  ) : null}
                </div>
              )}
            </div>
            <div className="flex flex-col gap-2">
              {!manualAnimeTvPath && tvLibraries && tvLibraries.length > 0 ? (
                <>
                  <select
                    aria-label="Anime TV library folder"
                    className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                    value={form.anime_tv_root ?? ''}
                    onChange={(e) => setForm((prev) => ({ ...prev, anime_tv_root: e.target.value }))}
                  >
                    <option value="">No anime TV library folder…</option>
                    {tvLibraries.map((lib) => (
                      <option
                        key={`${lib.section_key}:${lib.path}`}
                        value={lib.path}
                        disabled={lib.writable === false}
                      >
                        {lib.title} — {lib.path}
                        {lib.writable === false ? ' · not writable by the app' : ''}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="self-start text-xs text-gold hover:underline"
                    onClick={() => setManualAnimeTvPath(true)}
                  >
                    Use a custom path instead
                  </button>
                </>
              ) : (
                <div className="flex flex-col gap-2">
                  <Field
                    label="Anime TV library folder"
                    type="text"
                    placeholder="/library/anime-tv"
                    value={form.anime_tv_root ?? ''}
                    onChange={(e) => setForm((prev) => ({ ...prev, anime_tv_root: e.target.value }))}
                  />
                  {tvLibraries && tvLibraries.length > 0 ? (
                    <button
                      type="button"
                      className="self-start text-xs text-gold hover:underline"
                      onClick={() => setManualAnimeTvPath(false)}
                    >
                      ← Pick from a Plex library instead
                    </button>
                  ) : null}
                </div>
              )}
            </div>
          </div>
        )}
      </section>

      <div className="sticky bottom-0 mt-6 flex items-center justify-between gap-4 rounded-2xl border border-hairline bg-bg/90 p-4 backdrop-blur">
        <span className="font-mono text-xs text-faint">
          {verifiedCount}/{SERVICES.length} verified
        </span>
        <Button
          disabled={!allVerified || !setupTokenReady}
          loading={complete.isPending}
          onClick={() => void onComplete()}
        >
          Complete setup
        </Button>
      </div>
    </div>
  )
}
