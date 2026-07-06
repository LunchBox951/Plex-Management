import { type ReactNode, useRef, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import {
  useAuthMe,
  useCompleteSetup,
  useSetupStatus,
  useValidateService,
} from '../api/hooks'
import type { PlexLibraryOption, SetupCompleteRequest } from '../api/types'
import { clearSetupToken, getSetupToken, setSetupToken } from '../lib/apiKey'
import { type ApiError, isApiError, toApiError } from '../lib/errors'
import { cn } from '../lib/cn'
import { PlexLogin } from '../components/PlexLogin'
import { ServerPicker, type VerifiedServer } from '../components/setup/ServerPicker'
import { Button } from '../components/ui/Button'
import { Field } from '../components/ui/Field'
import { CenteredSpinner } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'

/**
 * The wizard is a three-step, derived state machine: sign in with Plex → pick the
 * server your account owns → configure the remaining services. There is no token
 * or key entry — the signed-in Plex session IS the credential, and setup mints
 * nothing to disclose (ADR-0016). Plex's URL/token/machine-identifier all come
 * from the verified server, never a typed card.
 */
type WizardStep = 'signin' | 'server' | 'services'

// The three service cards on the final step — Plex is NOT one of them (its
// connection is chosen + verified on the `server` step).
type ServiceKey = 'prowlarr' | 'qbittorrent' | 'tmdb'

// The typed service credentials the cards collect. Plex fields + the library
// roots live outside this (roots get their own pickers; Plex comes from the
// verified server), so every value here is a plain required `string`.
type ServicesFormKey =
  | 'prowlarr_url'
  | 'prowlarr_api_key'
  | 'qbittorrent_url'
  | 'qbittorrent_username'
  | 'qbittorrent_password'
  | 'tmdb_api_key'

interface FieldDef {
  key: ServicesFormKey
  label: string
  type: 'text' | 'password'
  placeholder?: string
}

interface ServiceDef {
  key: ServiceKey
  label: string
  blurb: string
  fields: FieldDef[]
}

const SERVICES: ServiceDef[] = [
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

type ServicesForm = Record<ServicesFormKey, string>

const EMPTY_SERVICES_FORM: ServicesForm = {
  prowlarr_url: '',
  prowlarr_api_key: '',
  qbittorrent_url: '',
  qbittorrent_username: '',
  qbittorrent_password: '',
  tmdb_api_key: '',
}

interface LibraryRoots {
  movies_root: string
  tv_root: string
  anime_movie_root: string
  anime_tv_root: string
}

const EMPTY_ROOTS: LibraryRoots = {
  movies_root: '',
  tv_root: '',
  anime_movie_root: '',
  anime_tv_root: '',
}

interface TestResult {
  ok: boolean
  message: string
}

type ResultsState = Record<ServiceKey, TestResult | null>
type PendingState = Record<ServiceKey, boolean>

const EMPTY_RESULTS: ResultsState = { prowlarr: null, qbittorrent: null, tmdb: null }
const EMPTY_TESTING: PendingState = { prowlarr: false, qbittorrent: false, tmdb: false }

// A rejection from `unwrap`/`ensureOk` is already a normalized ApiError; pass it
// through. Anything else (a bug, a non-envelope network throw) is routed through
// `toApiError` so the card renders a real, honest message rather than the
// `undefined` a bare `error as ApiError` cast would leave when there's no
// `.message` (matches ServerPicker/PlexLogin's `asDisplayError`).
function asApiError(error: unknown): ApiError {
  return isApiError(error) ? error : toApiError(error)
}

function bodyFor(service: ServiceKey, form: ServicesForm): Record<string, string> {
  switch (service) {
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
  // Auth is checked only once install-state has resolved to "not initialized":
  // the wizard is a pre-init surface, and an initialized install redirects away
  // below before this ever matters.
  const authMe = useAuthMe(status.data?.initialized === false)
  const validate = useValidateService()
  const complete = useCompleteSetup()

  const [server, setServer] = useState<VerifiedServer | null>(null)
  const [form, setForm] = useState<ServicesForm>(EMPTY_SERVICES_FORM)
  const [roots, setRoots] = useState<LibraryRoots>(EMPTY_ROOTS)
  const [results, setResults] = useState<ResultsState>(EMPTY_RESULTS)
  const [testing, setTesting] = useState<PendingState>(EMPTY_TESTING)
  // Seed from the PERSISTED token, not just an empty string: a mid-wizard reload
  // survives auth (the 30-day session cookie) but loses in-memory React state, so
  // an input that started empty would leave `setupTokenReady` false with no field
  // in sight — a dead end where Test/Complete are disabled but the token is still
  // sent from sessionStorage (north-star-#1 violation). Restoring the input from
  // getSetupToken() keeps the gate's source of truth aligned with what is actually
  // transmitted, and the token card (rendered on every step below) still lets a
  // fresh tab, whose per-tab sessionStorage is empty, (re)enter it.
  const [setupTokenInput, setSetupTokenInput] = useState(() => getSetupToken() ?? '')
  // Reveal a typed override instead of the Plex pick-list (split-mount / odd layout).
  const [manualPath, setManualPath] = useState(false)
  const [manualTvPath, setManualTvPath] = useState(false)
  const [manualAnimeMoviePath, setManualAnimeMoviePath] = useState(false)
  const [manualAnimeTvPath, setManualAnimeTvPath] = useState(false)
  // Per-service generation: bumped on every edit so an in-flight validation whose
  // fields changed underneath it is discarded (never marks stale creds verified).
  const validationGen = useRef<Record<ServiceKey, number>>({
    prowlarr: 0,
    qbittorrent: 0,
    tmdb: 0,
  })

  if (status.isLoading) return <CenteredSpinner label="Loading…" />

  // Already configured -> the wizard has nothing to do.
  if (status.data?.initialized) return <Navigate to="/" replace />

  const authed = authMe.data?.authenticated === true
  const step: WizardStep = !authed ? 'signin' : server === null ? 'server' : 'services'

  const setupTokenReady =
    status.data?.setup_token_required !== true || setupTokenInput.trim().length > 0

  // Rendered on EVERY step while required (not only sign-in): a post-reload or
  // fresh-tab operator lands on the server/services step already authed, and
  // without a reachable token field here they could neither re-enter the token
  // nor run Test/Complete — a terminal-only recovery (north star #1).
  const tokenCard = status.data?.setup_token_required ? (
    <section className="mb-4 rounded-2xl border border-hairline bg-surface p-5">
      <h2 className="font-display text-lg font-bold text-ink">Setup token</h2>
      <p className="mt-1 text-sm text-muted">
        Enter the one-time bootstrap token from your server's environment to continue setup.
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
  ) : null

  if (step === 'signin') {
    return (
      <Shell>
        {tokenCard}
        <PlexLogin onSignedIn={() => void authMe.refetch()} />
      </Shell>
    )
  }

  if (step === 'server' || server === null) {
    return (
      <Shell>
        {tokenCard}
        <ServerPicker onVerified={(verified) => setServer(verified)} />
      </Shell>
    )
  }

  // --- services step ---------------------------------------------------------
  const movieLibraries = server.libraries.filter((l) => l.section_type === 'movie')
  const tvLibraries = server.libraries.filter((l) => l.section_type === 'tv')

  const setField = (key: ServicesFormKey, value: string, service: ServiceKey) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    // Editing a field invalidates that service's prior test result + any in-flight one.
    setResults((prev) => ({ ...prev, [service]: null }))
    validationGen.current[service] += 1
  }

  const test = async (service: ServiceKey) => {
    const gen = validationGen.current[service]
    setTesting((prev) => ({ ...prev, [service]: true }))
    try {
      const res = await validate.mutateAsync({ service, body: bodyFor(service, form) })
      if (validationGen.current[service] !== gen) return // fields changed; ignore stale result
      setResults((prev) => ({ ...prev, [service]: { ok: res.ok, message: res.message } }))
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
  // Completion needs at least ONE library root — movies, tv, OR either anime root
  // (ADR-0015/ADR-0011): a movie-only, tv-only, or anime-only Plex is all legit.
  const hasLibraryRoot =
    roots.movies_root.trim() !== '' ||
    roots.tv_root.trim() !== '' ||
    roots.anime_movie_root.trim() !== '' ||
    roots.anime_tv_root.trim() !== ''
  const allVerified = servicesVerified && setupTokenReady && hasLibraryRoot

  const onComplete = async () => {
    try {
      if (status.data?.setup_token_required) {
        setSetupToken(setupTokenInput.trim())
      }
      const body: SetupCompleteRequest = {
        plex_url: server.url,
        plex_machine_identifier: server.machine_identifier,
        // A custom override, or `null` for the happy path — the backend reads a
        // null/absent token as "use the signed-in admin's stored OAuth token"
        // (setup.py: `if plex_token is None: … _admin_plex_token`), so no Plex
        // token is ever typed or shown on the owned-server path (ADR-0016).
        plex_token: server.token ?? null,
        prowlarr_url: form.prowlarr_url,
        prowlarr_api_key: form.prowlarr_api_key,
        qbittorrent_url: form.qbittorrent_url,
        qbittorrent_username: form.qbittorrent_username,
        qbittorrent_password: form.qbittorrent_password,
        tmdb_api_key: form.tmdb_api_key,
        movies_root: roots.movies_root,
        tv_root: roots.tv_root,
        anime_movie_root: roots.anime_movie_root,
        anime_tv_root: roots.anime_tv_root,
      }
      await complete.mutateAsync(body)
      // The bootstrap token is consumed; nothing is minted to reveal (ADR-0016) —
      // go straight to the app on the freshly-authenticated session.
      clearSetupToken()
      navigate('/', { replace: true })
    } catch (error) {
      toast({ title: 'Setup failed', description: asApiError(error).message, intent: 'error' })
    }
  }

  return (
    <Shell>
      {tokenCard}
      <section className="mb-4 flex items-center justify-between gap-3 rounded-2xl border border-available/40 bg-surface p-4">
        <span className="min-w-0 truncate text-sm text-ink">
          Plex: {server.url} — verified ✓
        </span>
        <button
          type="button"
          className="shrink-0 text-xs text-gold hover:underline"
          onClick={() => setServer(null)}
        >
          Change
        </button>
      </section>

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

      <LibrarySection
        title="Library"
        blurb="Where imported movies are placed — pick a folder Plex already watches."
        ariaLabel="Movies library folder"
        placeholder="/library/movies"
        chooseLabel="Choose a movie library folder…"
        emptyHint="Plex reports no movie library — leave this unset if you don't request movies, or enter a writable folder the app places movies into."
        value={roots.movies_root}
        onChange={(value) => setRoots((prev) => ({ ...prev, movies_root: value }))}
        libraries={movieLibraries}
        manual={manualPath}
        setManual={setManualPath}
        chosenBadge="✓ chosen"
      />

      <LibrarySection
        title="TV Library"
        blurb="Where imported tv seasons are placed — pick a folder Plex already watches. Leave unset if you don't request tv shows."
        ariaLabel="TV library folder"
        placeholder="/library/tv"
        chooseLabel="Skip TV for now…"
        emptyHint="Plex reports no tv library — enter the folder the app writes tv into (it must be writable), or leave blank to skip TV."
        value={roots.tv_root}
        onChange={(value) => setRoots((prev) => ({ ...prev, tv_root: value }))}
        libraries={tvLibraries}
        manual={manualTvPath}
        setManual={setManualTvPath}
        optional
      />

      {/* Anime library routing (ADR-0015) — both roots OPTIONAL and never gate
          completion: unset, anime imports fall back to the Movies/TV roots. */}
      <section className="mt-4 rounded-2xl border border-hairline bg-surface p-5 transition-colors">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-display text-lg font-bold text-ink">Anime library</h2>
          <span className="font-mono text-xs text-faint">optional</span>
        </div>
        <p className="mt-1 text-sm text-muted">
          Route anime movies/episodes to a separate Plex library instead of the Movies/TV folders
          above. Leave unset to keep anime in the normal libraries.
        </p>
        <div className="mt-4 flex flex-col gap-4">
          <RootPicker
            ariaLabel="Anime movies library folder"
            placeholder="/library/anime-movies"
            chooseLabel="No anime movies library folder…"
            value={roots.anime_movie_root}
            onChange={(value) => setRoots((prev) => ({ ...prev, anime_movie_root: value }))}
            libraries={movieLibraries}
            manual={manualAnimeMoviePath}
            setManual={setManualAnimeMoviePath}
          />
          <RootPicker
            ariaLabel="Anime TV library folder"
            placeholder="/library/anime-tv"
            chooseLabel="No anime TV library folder…"
            value={roots.anime_tv_root}
            onChange={(value) => setRoots((prev) => ({ ...prev, anime_tv_root: value }))}
            libraries={tvLibraries}
            manual={manualAnimeTvPath}
            setManual={setManualAnimeTvPath}
          />
        </div>
      </section>

      <div className="sticky bottom-0 mt-6 flex items-center justify-between gap-4 rounded-2xl border border-hairline bg-bg/90 p-4 backdrop-blur">
        <span className="font-mono text-xs text-faint">
          {verifiedCount}/{SERVICES.length} verified
        </span>
        <Button
          disabled={!allVerified}
          loading={complete.isPending}
          onClick={() => void onComplete()}
        >
          Complete setup
        </Button>
      </div>
    </Shell>
  )
}

/** The shared wizard chrome (logo + heading) wrapping every step's body. */
function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto max-w-2xl px-5 py-12">
      <header className="mb-8 text-center">
        <div className="font-display text-2xl font-extrabold tracking-wide">
          PLEX<span className="text-gold">MGR</span>
        </div>
        <h1 className="mt-4 font-display text-3xl font-extrabold">Welcome — let's connect things</h1>
        <p className="mt-2 text-muted">
          Sign in with Plex, pick your server, then connect the rest. You never touch a terminal.
        </p>
      </header>
      {children}
    </div>
  )
}

interface RootPickerProps {
  ariaLabel: string
  placeholder: string
  chooseLabel?: string
  value: string
  onChange: (value: string) => void
  libraries: PlexLibraryOption[]
  manual: boolean
  setManual: (manual: boolean) => void
}

/**
 * A single library-root chooser: a pick-list of the folders Plex reports for
 * this section type, or a typed override (`manual`) for a split-mount / odd
 * layout Plex can't enumerate. Shared by the Movies, TV, and both anime roots.
 */
function RootPicker({
  ariaLabel,
  placeholder,
  chooseLabel,
  value,
  onChange,
  libraries,
  manual,
  setManual,
}: RootPickerProps) {
  if (!manual && libraries.length > 0) {
    return (
      <div className="flex flex-col gap-2">
        <select
          aria-label={ariaLabel}
          className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">{chooseLabel ?? 'Choose a library folder…'}</option>
          {libraries.map((lib) => (
            <option key={`${lib.section_key}:${lib.path}`} value={lib.path} disabled={lib.writable === false}>
              {lib.title} — {lib.path}
              {lib.writable === false ? ' · not writable by the app' : ''}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="self-start text-xs text-gold hover:underline"
          onClick={() => setManual(true)}
        >
          Use a custom path instead
        </button>
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <Field
        label={ariaLabel}
        type="text"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {libraries.length > 0 ? (
        <button
          type="button"
          className="self-start text-xs text-gold hover:underline"
          onClick={() => setManual(false)}
        >
          ← Pick from a Plex library instead
        </button>
      ) : null}
    </div>
  )
}

interface LibrarySectionProps extends RootPickerProps {
  title: string
  blurb: string
  emptyHint: string
  optional?: boolean
  chosenBadge?: string
}

/** A titled card wrapping a {@link RootPicker} — the Movies + TV sections. */
function LibrarySection({
  title,
  blurb,
  emptyHint,
  optional,
  chosenBadge,
  ...picker
}: LibrarySectionProps) {
  const chosen = picker.value.trim() !== ''
  return (
    <section
      className={cn(
        'mt-4 rounded-2xl border bg-surface p-5 transition-colors',
        chosen ? 'border-available/40' : 'border-hairline',
      )}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="font-display text-lg font-bold text-ink">{title}</h2>
        {chosen ? (
          <span className="font-mono text-xs text-available">{chosenBadge ?? '✓ chosen'}</span>
        ) : optional ? (
          <span className="font-mono text-xs text-faint">optional</span>
        ) : null}
      </div>
      <p className="mt-1 text-sm text-muted">{blurb}</p>
      <div className="mt-4">
        {picker.libraries.length > 0 || picker.manual ? (
          <RootPicker {...picker} />
        ) : (
          <>
            <RootPicker {...picker} />
            <p className="mt-2 text-xs text-faint">{emptyHint}</p>
          </>
        )}
      </div>
    </section>
  )
}
