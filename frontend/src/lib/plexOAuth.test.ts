import { afterEach, describe, expect, it, vi } from 'vitest'
import { PlexPinError, openPlexPopup, plexClientId, runPlexPinFlow } from './plexOAuth'

/** A plex.tv v2 pin body, as returned by both POST /pins and GET /pins/{id}. */
function pinBody(overrides: Record<string, unknown> = {}): Response {
  const data = {
    id: 42,
    code: 'ABCD',
    expiresIn: 1800,
    authToken: null,
    ...overrides,
  }
  return { ok: true, status: 200, json: async () => data } as unknown as Response
}

/** A stand-in for the popup Window handle the click handler pre-opened. */
function makePopup(): Window {
  return {
    closed: false,
    close: vi.fn(),
    location: { href: '' },
  } as unknown as Window
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('plexClientId', () => {
  it('reads a persisted id from localStorage under the plexmgr.plexClientId key', () => {
    // localStorage isn't provided in this jsdom config (the module degrades to an
    // in-memory fallback, like apiKey.ts) — stub a real store to pin the key name
    // and the read-first contract independently of test order.
    const store: Record<string, string> = { 'plexmgr.plexClientId': 'persisted-uuid' }
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => store[key] ?? null,
      setItem: (key: string, value: string) => {
        store[key] = value
      },
      removeItem: (key: string) => {
        delete store[key]
      },
    })
    expect(plexClientId()).toBe('persisted-uuid')
  })

  it('returns a stable, non-empty id across calls', () => {
    const first = plexClientId()
    expect(first).not.toBe('')
    expect(plexClientId()).toBe(first)
  })
})

describe('openPlexPopup', () => {
  it('opens the branded loading route synchronously and returns the window handle', () => {
    const popup = makePopup()
    const open = vi.spyOn(window, 'open').mockReturnValue(popup)
    expect(openPlexPopup()).toBe(popup)
    expect(open).toHaveBeenCalledTimes(1)
    // The popup shows the in-app branded spinner (`/login/plex/loading`) until
    // runPlexPinFlow navigates it to plex.tv — never a blank `about:blank` frame.
    expect(open.mock.calls[0]?.[0]).toBe('/login/plex/loading')
  })

  it('returns null when the browser blocks the popup', () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    expect(openPlexPopup()).toBeNull()
  })
})

describe('runPlexPinFlow', () => {
  it('resolves the auth token and closes the popup once the PIN is approved', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(pinBody({ id: 7, code: 'WXYZ' })) // create
      .mockResolvedValueOnce(pinBody({ id: 7, code: 'WXYZ' })) // poll 1: not yet
      .mockResolvedValueOnce(pinBody({ id: 7, code: 'WXYZ', authToken: 'tok-xyz' })) // poll 2: approved
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    await vi.advanceTimersByTimeAsync(1000) // poll 1
    await vi.advanceTimersByTimeAsync(1000) // poll 2

    await expect(promise).resolves.toBe('tok-xyz')
    expect(popup.close).toHaveBeenCalledTimes(1)
  })

  it('issues the exact plex.tv v2 create + poll requests and navigates the popup', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(pinBody({ id: 77, code: 'PQRS' })) // create
      .mockResolvedValueOnce(pinBody({ id: 77, code: 'PQRS', authToken: 'tok' })) // poll: approved
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    await vi.advanceTimersByTimeAsync(1000)
    await expect(promise).resolves.toBe('tok')

    const clientId = plexClientId()
    const [createUrl, createInit] = fetch.mock.calls[0] as [string, RequestInit]
    expect(createUrl).toBe('https://plex.tv/api/v2/pins?strong=true')
    expect(createInit.method).toBe('POST')
    expect(createInit.headers).toMatchObject({
      'X-Plex-Product': 'Plex Manager',
      'X-Plex-Client-Identifier': clientId,
      Accept: 'application/json',
    })

    const [pollUrl, pollInit] = fetch.mock.calls[1] as [string, RequestInit]
    expect(pollUrl).toBe('https://plex.tv/api/v2/pins/77')
    expect(pollInit.headers).toMatchObject({
      'X-Plex-Product': 'Plex Manager',
      'X-Plex-Client-Identifier': clientId,
      Accept: 'application/json',
    })

    expect(popup.location.href).toBe(
      `https://app.plex.tv/auth#?clientID=${clientId}&code=PQRS&context%5Bdevice%5D%5Bproduct%5D=Plex%20Manager`,
    )
  })

  it('rejects with plex_popup_blocked when handed a null popup', async () => {
    await expect(runPlexPinFlow(null)).rejects.toBeInstanceOf(PlexPinError)
    await expect(runPlexPinFlow(null)).rejects.toMatchObject({ code: 'plex_popup_blocked' })
  })

  it('rejects with plex_popup_closed when the user closes the popup mid-poll', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    const fetch = vi.fn().mockResolvedValue(pinBody())
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    const assertion = expect(promise).rejects.toMatchObject({ code: 'plex_popup_closed' })
    ;(popup as unknown as { closed: boolean }).closed = true
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
  })

  it('rejects with plex_tv_unreachable_browser when the create fetch rejects', async () => {
    const popup = makePopup()
    const fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', fetch)

    await expect(runPlexPinFlow(popup)).rejects.toMatchObject({
      code: 'plex_tv_unreachable_browser',
    })
  })

  it('rejects with plex_tv_unreachable_browser when a poll fetch rejects', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(pinBody({ id: 5 })) // create ok
      .mockRejectedValueOnce(new TypeError('Failed to fetch')) // poll rejects
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    const assertion = expect(promise).rejects.toMatchObject({
      code: 'plex_tv_unreachable_browser',
    })
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
  })

  it('rejects with plex_pin_expired when the PIN is never approved before expiry', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    // expiresIn: 3s — deadline is three poll cycles out; never carries a token.
    const fetch = vi.fn().mockResolvedValue(pinBody({ id: 9, expiresIn: 3 }))
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    const assertion = expect(promise).rejects.toMatchObject({ code: 'plex_pin_expired' })
    await vi.advanceTimersByTimeAsync(4000)
    await assertion
  })

  it('closes the popup even when the flow ends in failure', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    // Never approved; the deadline is one poll out, so the flow expires and must
    // still tidy up the popup it was handed (not just on the success path).
    const fetch = vi.fn().mockResolvedValue(pinBody({ id: 9, expiresIn: 1 }))
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    const assertion = expect(promise).rejects.toMatchObject({ code: 'plex_pin_expired' })
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
    expect(popup.close).toHaveBeenCalledTimes(1)
  })

  it('falls back to a finite expiry when plex.tv omits a usable expiresIn (no NaN infinite poll)', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    // A malformed create response (expiresIn not a finite number) must not leave
    // the deadline as NaN — the guard falls back to 1800s so expiry still fires.
    const fetch = vi.fn().mockResolvedValue(pinBody({ id: 9, expiresIn: Number.NaN }))
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    const assertion = expect(promise).rejects.toMatchObject({ code: 'plex_pin_expired' })
    await vi.advanceTimersByTimeAsync(1_800_000)
    await assertion
  })

  it('treats an empty-string authToken as not-yet-approved', async () => {
    vi.useFakeTimers()
    const popup = makePopup()
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(pinBody({ id: 3 })) // create
      .mockResolvedValueOnce(pinBody({ id: 3, authToken: '' })) // poll 1: empty, not approved
      .mockResolvedValueOnce(pinBody({ id: 3, authToken: 'tok-real' })) // poll 2: approved
    vi.stubGlobal('fetch', fetch)

    const promise = runPlexPinFlow(popup)
    await vi.advanceTimersByTimeAsync(1000) // poll 1: '' must NOT resolve the flow
    await vi.advanceTimersByTimeAsync(1000) // poll 2: real token resolves

    await expect(promise).resolves.toBe('tok-real')
  })
})
