/**
 * Browser-side plex.tv PIN client — Overseerr's popup + poll pattern.
 *
 * The backend does ALL token verification (`POST /api/v1/auth/plex` takes an
 * `auth_token`). The browser's only job is the plex.tv "PIN" dance that yields
 * that token:
 *
 *   1. pre-open a popup SYNCHRONOUSLY from the click handler — popup blockers
 *      only permit `window.open` in direct response to a user gesture, so this
 *      must happen before any `await` ({@link openPlexPopup});
 *   2. create a strong PIN on plex.tv;
 *   3. navigate the popup to plex.tv's hosted login for that PIN;
 *   4. poll the PIN once a second until it carries an `authToken`, or a terminal
 *      failure occurs.
 *
 * Every terminal failure is one of four typed, retryable {@link PlexPinFailure}
 * codes — never a raw `Error`. The plex.tv auth token is never logged nor placed
 * in an error message.
 */

export type PlexPinFailure =
  | 'plex_popup_blocked'
  | 'plex_popup_closed'
  | 'plex_pin_expired'
  | 'plex_tv_unreachable_browser'

export class PlexPinError extends Error {
  constructor(public readonly code: PlexPinFailure) {
    super(code)
    this.name = 'PlexPinError'
  }
}

const CLIENT_ID_KEY = 'plexmgr.plexClientId'
const PLEX_PRODUCT = 'Plex Manager'
const POLL_INTERVAL_MS = 1000

let memoryClientId: string | null = null

/**
 * A stable, per-install client identifier persisted in localStorage. It MUST be
 * identical for the PIN create and every subsequent poll, so an in-memory
 * fallback keeps it stable for the session even when localStorage is unavailable
 * (private mode / locked-down browsers) — regenerating between create and poll
 * would strand the flow.
 */
export function plexClientId(): string {
  try {
    const stored = localStorage.getItem(CLIENT_ID_KEY)
    if (stored !== null && stored !== '') return stored
  } catch {
    /* storage unreadable — fall through to the in-memory copy */
  }
  if (memoryClientId !== null) return memoryClientId
  const id = crypto.randomUUID()
  memoryClientId = id
  try {
    localStorage.setItem(CLIENT_ID_KEY, id)
  } catch {
    /* private-mode / storage-disabled: the in-memory copy carries the session */
  }
  return id
}

/**
 * Pre-open the auth popup. MUST be called synchronously from the click handler,
 * before any `await`, or popup blockers will null it. Returns `null` when
 * blocked; {@link runPlexPinFlow} maps that to `plex_popup_blocked`.
 */
export function openPlexPopup(): Window | null {
  return window.open('about:blank', 'plex-auth', 'width=600,height=700')
}

interface PlexPinResponse {
  id: number
  code: string
  expiresIn: number
  authToken: string | null
}

interface PlexPin {
  id: number
  code: string
  expiresIn: number
}

function plexHeaders(): Record<string, string> {
  return {
    'X-Plex-Product': PLEX_PRODUCT,
    'X-Plex-Client-Identifier': plexClientId(),
    Accept: 'application/json',
  }
}

async function createPin(): Promise<PlexPin> {
  let body: PlexPinResponse
  try {
    const res = await fetch('https://plex.tv/api/v2/pins?strong=true', {
      method: 'POST',
      headers: plexHeaders(),
    })
    body = (await res.json()) as PlexPinResponse
  } catch {
    throw new PlexPinError('plex_tv_unreachable_browser')
  }
  return { id: body.id, code: body.code, expiresIn: body.expiresIn }
}

async function readPinToken(id: number): Promise<string | null> {
  let body: PlexPinResponse
  try {
    const res = await fetch(`https://plex.tv/api/v2/pins/${encodeURIComponent(String(id))}`, {
      headers: plexHeaders(),
    })
    body = (await res.json()) as PlexPinResponse
  } catch {
    throw new PlexPinError('plex_tv_unreachable_browser')
  }
  return body.authToken ?? null
}

function authPopupUrl(code: string): string {
  // `context[device][product]=Plex Manager` is pre-encoded per plex.tv's hosted
  // login contract (the square brackets and space stay percent-encoded verbatim).
  return (
    `https://app.plex.tv/auth#?clientID=${encodeURIComponent(plexClientId())}` +
    `&code=${encodeURIComponent(code)}` +
    `&context%5Bdevice%5D%5Bproduct%5D=Plex%20Manager`
  )
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}

/**
 * Create the PIN, point the popup at plex.tv's hosted login, and poll once a
 * second until the PIN carries an `authToken`. Resolves with that token (closing
 * the popup) or rejects with a {@link PlexPinError} carrying one of the four
 * terminal {@link PlexPinFailure} codes.
 */
export async function runPlexPinFlow(popup: Window | null): Promise<string> {
  if (popup === null) {
    throw new PlexPinError('plex_popup_blocked')
  }
  const pin = await createPin()
  popup.location.href = authPopupUrl(pin.code)
  const expiresAt = Date.now() + pin.expiresIn * 1000
  for (;;) {
    await delay(POLL_INTERVAL_MS)
    if (popup.closed) {
      throw new PlexPinError('plex_popup_closed')
    }
    if (Date.now() >= expiresAt) {
      throw new PlexPinError('plex_pin_expired')
    }
    const token = await readPinToken(pin.id)
    if (token !== null) {
      popup.close()
      return token
    }
  }
}
