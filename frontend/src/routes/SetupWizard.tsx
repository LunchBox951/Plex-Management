import { type ReactNode, type Ref, useEffect, useRef, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import {
  useAuthMe,
  useCompleteSetup,
  useSetupStatus,
  useValidateService,
} from '../api/hooks'
import type { PlexLibraryOption, SetupCompleteRequest } from '../api/types'
import { libraryOptionNote, libraryOptionValue } from '../api/types'
import { clearSetupToken, getSetupToken, setSetupToken } from '../lib/setupToken'
import { type ApiError, isApiError, toApiError } from '../lib/errors'
import { cn } from '../lib/cn'
import { PlexLogin } from '../components/PlexLogin'
import { ServerPicker, type VerifiedServer } from '../components/setup/ServerPicker'
import { Button } from '../components/ui/Button'
import { Field } from '../components/ui/Field'
import { CenteredSpinner } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'

/**
 * The wizard is a five-step presentation over prerequisite-derived state: sign
 * in with Plex → pick an owned server → validate services → choose library roots
 * → finish. Authentication and server verification still derive the visible
 * step; only the services/libraries split and this tab's successful completion
 * are local presentation state. Setup mints nothing to disclose (ADR-0016), and
 * the URL/token/machine-identifier all come from the verified server.
 */
type WizardStep = 'signin' | 'server' | 'services' | 'libraries' | 'done'

/** Ordered setup copy used by both the shell and its contract tests. */
// eslint-disable-next-line react-refresh/only-export-components -- tests import the canonical step contract.
export const WIZARD_STEPS: ReadonlyArray<{
  id: WizardStep
  label: string
  heading: string
  description: string
}> = [
  {
    id: 'signin',
    label: 'Sign in',
    heading: 'Sign in with Plex',
    description:
      'Plex is the identity provider, so the server owner administers Plex Manager and shared users get request access automatically.',
  },
  {
    id: 'server',
    label: 'Server',
    heading: 'Pick your server',
    description:
      'Choose one of the servers your Plex account can reach, with local connections preferred.',
  },
  {
    id: 'services',
    label: 'Services',
    heading: 'Connect services',
    description:
      'Prowlarr finds releases and qBittorrent downloads them, so both must be validated before you continue.',
  },
  {
    id: 'libraries',
    label: 'Libraries',
    heading: 'Confirm library roots',
    description:
      'Choose where finished files land, using roots that are writable from inside the container.',
  },
  {
    id: 'done',
    label: 'Done',
    heading: "You're set",
    description: 'Setup is complete.',
  },
]

// The three service panels on the services step — Plex is NOT one of them (its
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
  // qBittorrent only (issues #133/#157): a NON-blocking, informational note —
  // e.g. the client's default save path isn't visible inside this container.
  // Never flips `ok`; `undefined` for every other service and whenever the
  // backend didn't set one.
  note?: string
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
  const [configurationStep, setConfigurationStep] = useState<'services' | 'libraries'>(
    'services',
  )
  const [completedHere, setCompletedHere] = useState(false)
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
  // transmitted, and the token section (rendered on every step below) still lets a
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
  const headingRef = useRef<HTMLHeadingElement>(null)
  const previousStep = useRef<WizardStep | null>(null)

  const authed = authMe.data?.authenticated === true
  const step: WizardStep = !authed
    ? 'signin'
    : completedHere
      ? 'done'
      : server === null
        ? 'server'
        : configurationStep

  // Focus only real step transitions. Query updates, field edits, and validation
  // results keep focus where the operator left it.
  useEffect(() => {
    if (status.isLoading || (status.data?.initialized && !completedHere)) return
    if (previousStep.current !== null && previousStep.current !== step) {
      headingRef.current?.focus()
    }
    previousStep.current = step
  }, [completedHere, status.data?.initialized, status.isLoading, step])

  if (status.isLoading) return <CenteredSpinner label="Loading…" />

  // A successful completion in this tab gets to show Done. Direct visits and
  // reloads after initialization still leave the one-shot wizard immediately.
  if (status.data?.initialized && !completedHere) return <Navigate to="/" replace />

  const setupTokenReady =
    status.data?.setup_token_required !== true || setupTokenInput.trim().length > 0

  // Rendered on EVERY pre-completion step while required: a post-reload or
  // fresh-tab operator lands on the server/services step already authed, and
  // without a reachable token field here they could neither re-enter the token
  // nor run Test/Complete — a terminal-only recovery (north star #1).
  const tokenField = status.data?.setup_token_required ? (
    <SetupTokenField
      value={setupTokenInput}
      onChange={(value) => {
        setSetupTokenInput(value)
        if (value.trim()) {
          setSetupToken(value.trim())
        } else {
          clearSetupToken()
        }
      }}
    />
  ) : null

  if (step === 'signin') {
    return (
      <WizardShell step={step}>
        <StepCard step={step} headingRef={headingRef}>
          {tokenField}
          <PlexLogin embedded onSignedIn={() => void authMe.refetch()} />
        </StepCard>
      </WizardShell>
    )
  }

  if (step === 'server' || server === null) {
    return (
      <WizardShell step="server">
        <StepCard step="server" headingRef={headingRef}>
          {tokenField}
          <ServerPicker
            embedded
            onVerified={(verified) => {
              setConfigurationStep('services')
              setServer(verified)
            }}
            // Gate + key the owned-servers discovery on the setup token: a fresh
            // tab lands here already authed but with an empty per-tab token, so
            // firing discovery now would 401 and cache (retry:false). `setupTokenReady`
            // is false until the token is (re)entered; passing the trimmed value keys
            // the query so entering/correcting it triggers a fresh fetch, not a reload.
            setupTokenReady={setupTokenReady}
            setupToken={setupTokenInput.trim()}
          />
        </StepCard>
      </WizardShell>
    )
  }

  // --- post-server setup -----------------------------------------------------
  const movieLibraries = server.libraries.filter((l) => l.section_type === 'movie')
  const tvLibraries = server.libraries.filter((l) => l.section_type === 'tv')

  const setField = (key: ServicesFormKey, value: string, service: ServiceKey) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    // Editing a field invalidates that service's prior test result + any in-flight
    // one — clear BOTH the result and the pending flag now, synchronously, so the
    // Test connection button re-enables immediately rather than waiting on a
    // request that's about to be discarded as stale.
    setResults((prev) => ({ ...prev, [service]: null }))
    setTesting((prev) => ({ ...prev, [service]: false }))
    validationGen.current[service] += 1
  }

  const test = async (service: ServiceKey) => {
    const gen = validationGen.current[service]
    setTesting((prev) => ({ ...prev, [service]: true }))
    try {
      const res = await validate.mutateAsync({ service, body: bodyFor(service, form) })
      if (validationGen.current[service] !== gen) return // fields changed; ignore stale result
      setResults((prev) => ({
        ...prev,
        [service]: { ok: res.ok, message: res.message, note: res.download_path_note ?? undefined },
      }))
    } catch (error) {
      if (validationGen.current[service] !== gen) return
      setResults((prev) => ({
        ...prev,
        [service]: { ok: false, message: asApiError(error).message },
      }))
    } finally {
      // Generation-gated: a stale request settling after a newer edit/retest must
      // not clear the pending flag out from under that newer, still-in-flight
      // request (which would let its Test button appear enabled mid-request).
      if (validationGen.current[service] === gen) {
        setTesting((prev) => ({ ...prev, [service]: false }))
      }
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
        // A required explicit token for a custom URL, or `null` for a connection
        // advertised by plex.tv. Only that advertised-server path may reuse the
        // signed-in admin's stored OAuth token, so it never needs to show or
        // re-type the token (ADR-0016).
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
      // The bootstrap token is consumed; nothing is minted to reveal (ADR-0016).
      // Keep this successful tab in the wizard long enough to show the Done step.
      clearSetupToken()
      setCompletedHere(true)
    } catch (error) {
      toast({ title: 'Setup failed', description: asApiError(error).message, intent: 'error' })
    }
  }

  // Returning to the server step clears the picked library roots: they were chosen
  // from THIS server's reported libraries, so a different server's roots could be
  // paths it doesn't own — submitting them would write against the wrong server.
  // They must be re-picked from the NEW server's validate response, so also reset
  // the manual-path toggles back to the pick-list (ADR-0015 roots).
  const changeServer = () => {
    setConfigurationStep('services')
    setServer(null)
    setRoots(EMPTY_ROOTS)
    setManualPath(false)
    setManualTvPath(false)
    setManualAnimeMoviePath(false)
    setManualAnimeTvPath(false)
  }

  if (step === 'done') {
    return (
      <WizardShell step={step}>
        <StepCard step={step} headingRef={headingRef}>
          <div className="py-5 text-center" aria-hidden="true">
            <span className="inline-flex size-13 items-center justify-center rounded-full bg-available/15 text-2xl font-bold text-available">
              ✓
            </span>
          </div>
          <div className="mt-2 flex justify-end border-t border-hairline pt-5">
            <Button className="w-full sm:w-auto" onClick={() => navigate('/', { replace: true })}>
              Open Discover
            </Button>
          </div>
        </StepCard>
      </WizardShell>
    )
  }

  if (step === 'services') {
    return (
      <WizardShell step={step}>
        <StepCard step={step} headingRef={headingRef}>
          {tokenField}
          <div className="mt-6 rounded-xl border border-available/40 bg-bg/40 p-4">
            <span className="block text-sm text-ink">Plex server verified ✓</span>
          </div>

          <div className="mt-4 flex flex-col gap-4">
            {SERVICES.map((service) => {
              const result = results[service.key]
              return (
                <section
                  key={service.key}
                  className={cn(
                    'rounded-xl border bg-bg/40 p-5',
                    result?.ok ? 'border-available/40' : 'border-hairline',
                  )}
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-3">
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

                  <div className="mt-4 flex flex-col items-start gap-3 sm:flex-row sm:items-center">
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
                      <span className="min-w-0 break-words text-sm text-error">{result.message}</span>
                    ) : result?.ok ? (
                      <span className="min-w-0 break-words text-sm text-available">
                        {result.message}
                      </span>
                    ) : null}
                  </div>
                  {result?.note ? (
                    // Non-blocking, informational — this never gates `allVerified`
                    // (the check above only ever reads `result.ok`).
                    <p className="mt-2 break-words text-xs text-searching">⚠ {result.note}</p>
                  ) : null}
                </section>
              )
            })}
          </div>

          <div className="mt-6 border-t border-hairline pt-5 sm:flex sm:items-center sm:justify-between sm:gap-4">
            <span className="block font-mono text-xs text-faint">
              {verifiedCount}/{SERVICES.length} verified
            </span>
            <div className="mt-4 flex flex-col-reverse gap-3 sm:mt-0 sm:flex-row">
              <Button className="w-full sm:w-auto" variant="secondary" onClick={changeServer}>
                Back
              </Button>
              <Button
                className="w-full sm:w-auto"
                disabled={!servicesVerified || !setupTokenReady}
                onClick={() => setConfigurationStep('libraries')}
              >
                Continue
              </Button>
            </div>
          </div>
        </StepCard>
      </WizardShell>
    )
  }

  return (
    <WizardShell step="libraries">
      <StepCard step="libraries" headingRef={headingRef}>
        {tokenField}
        <p className="mt-6 break-words text-xs text-faint">
          Folders must be visible to <strong>this</strong> Plex Manager server. If it runs in
          Docker, pick a path under a mounted volume (usually <code>/media/…</code>).
        </p>

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
        <section className="mt-4 rounded-xl border border-hairline bg-bg/40 p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-3">
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

        <div className="mt-6 flex flex-col-reverse gap-3 border-t border-hairline pt-5 sm:flex-row sm:justify-between">
          <Button
            className="w-full sm:w-auto"
            variant="secondary"
            onClick={() => setConfigurationStep('services')}
          >
            Back
          </Button>
          <Button
            className="w-full sm:w-auto"
            disabled={!allVerified}
            loading={complete.isPending}
            onClick={() => void onComplete()}
          >
            Complete setup
          </Button>
        </div>
      </StepCard>
    </WizardShell>
  )
}

function WizardShell({ step, children }: { step: WizardStep; children: ReactNode }) {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-bg bg-[radial-gradient(ellipse_900px_500px_at_50%_-10%,rgba(231,194,125,0.07),transparent)] px-4 py-8 sm:px-6 sm:py-10">
      <div className="w-full max-w-[640px]">
        <header className="mb-7 text-center">
          <div className="font-display text-[22px] font-extrabold tracking-wide text-ink">
            PLEX<span className="text-gold">MGR</span>
          </div>
          <div className="mt-2 text-[13px] text-faint">First-run setup</div>
        </header>
        <StepIndicator step={step} />
        {children}
      </div>
    </main>
  )
}

function StepIndicator({ step }: { step: WizardStep }) {
  const currentIndex = WIZARD_STEPS.findIndex((item) => item.id === step)
  return (
    <div className="mb-6 overflow-x-auto pb-1">
      <ol aria-label="Setup progress" className="flex w-max min-w-full gap-1.5 sm:justify-center">
        {WIZARD_STEPS.map((item, index) => {
          const completed = index < currentIndex
          const current = index === currentIndex
          return (
            <li
              key={item.id}
              aria-current={current ? 'step' : undefined}
              aria-label={completed ? `${item.label}, completed` : undefined}
              className={cn(
                'flex items-center gap-2 whitespace-nowrap rounded-full px-3 py-2 text-xs font-semibold',
                current && 'bg-gold/10 text-gold ring-1 ring-inset ring-gold/30',
                completed && 'text-available',
                !current && !completed && 'text-faint',
              )}
            >
              <span
                aria-hidden={completed ? true : undefined}
                className={cn(
                  'inline-flex size-[18px] items-center justify-center rounded-full font-mono text-[10px] font-bold',
                  current && 'bg-gold text-gold-ink',
                  completed && 'bg-available/20 text-available',
                  !current && !completed && 'bg-white/8 text-muted',
                )}
              >
                {completed ? '✓' : index + 1}
              </span>
              <span>{item.label}</span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function StepCard({
  step,
  headingRef,
  children,
}: {
  step: WizardStep
  headingRef: Ref<HTMLHeadingElement>
  children: ReactNode
}) {
  const metadata = WIZARD_STEPS.find((item) => item.id === step)
  if (!metadata) return null
  const headingId = `setup-${step}-heading`
  const descriptionId = `setup-${step}-description`
  return (
    <section
      className="rounded-[14px] border border-hairline bg-surface p-5 shadow-2xl shadow-black/20 sm:p-[30px]"
      aria-labelledby={headingId}
      aria-describedby={descriptionId}
    >
      <h1
        ref={headingRef}
        id={headingId}
        tabIndex={-1}
        className="font-display text-xl font-extrabold text-ink outline-none"
      >
        {metadata.heading}
      </h1>
      <p id={descriptionId} className="mt-1.5 text-sm leading-6 text-muted">
        {metadata.description}
      </p>
      {children}
    </section>
  )
}

function SetupTokenField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <section className="mt-6 rounded-xl border border-hairline bg-bg/40 p-4">
      <h2 className="font-display text-base font-bold text-ink">Setup token</h2>
      <p className="mt-1 text-sm text-muted">
        Enter the one-time bootstrap token from your server's environment to continue setup.
      </p>
      <div className="mt-4">
        <Field
          label="Setup token"
          type="password"
          autoComplete="off"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
    </section>
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
          className="h-11 w-full min-w-0 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">{chooseLabel ?? 'Choose a library folder…'}</option>
          {libraries.map((lib) => (
            <option
              key={`${lib.section_key}:${lib.path}`}
              value={libraryOptionValue(lib)}
              disabled={lib.writable === false}
            >
              {lib.title} — {lib.path}
              {libraryOptionNote(lib)}
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

/** A titled inset panel wrapping a {@link RootPicker} — the Movies + TV sections. */
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
        'mt-4 rounded-xl border bg-bg/40 p-5',
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
