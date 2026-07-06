import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { toApiError } from '../lib/errors'
import { PlexPinError } from '../lib/plexOAuth'
import { AuthErrorCard } from './AuthErrorCard'

/**
 * The single source of truth for auth/setup failure copy. Every backend `detail`
 * code (plus the browser-side {@link PlexPinError} codes) maps to ONE exact,
 * actionable sentence — asserted verbatim so a copy drift fails the build. The
 * generic catch-all sentence is intentionally absent (north star #3).
 */
const COPY = {
  plex_tv_unreachable_browser:
    "Your browser couldn't reach plex.tv. Check your connection and any ad blockers, then try again.",
  plex_tv_unreachable_server:
    "Plex Manager's server couldn't reach plex.tv. plex.tv may be down, or the server has no internet access.",
  plex_popup_blocked:
    'Your browser blocked the Plex sign-in popup. Allow popups for this site and try again.',
  plex_popup_closed: 'The Plex sign-in window was closed before finishing. Try again.',
  plex_pin_expired: 'The Plex sign-in expired. Try again.',
  plex_token_invalid: 'plex.tv rejected the sign-in token. Sign in again.',
  no_owned_servers:
    'Your Plex account does not own any Plex Media Server. Sign in with the account that owns the server this app should manage.',
  setup_already_claimed:
    'Setup was already started by a different Plex account. Finish setup from that account, or reset the database.',
  server_not_owned:
    'Your Plex account does not own this server. Pick a server you own, or sign in as the owner.',
  server_unreachable_from_backend:
    "Plex Manager's server can't reach this address (your browser reaching it isn't enough). If Plex Manager runs in Docker, localhost points at the container — use the host's IP or host.docker.internal.",
  server_identity_failed: 'That address answered, but not like a Plex server. Check the URL.',
  server_access_denied:
    "Your Plex account doesn't have access to this server. Ask the owner to share it with you.",
  session_expired: 'Your session expired. Sign in again.',
  session_required: 'Sign in with Plex to continue.',
  sign_in_throttled: 'Too many sign-in attempts. Wait a minute and try again.',
  plex_account_required:
    'Server discovery needs a Plex-signed-in admin. Sign in with Plex first.',
  app_key_not_set: 'No recovery key exists. Generate one from Settings → Access.',
  plex_tv_bad_response:
    'plex.tv answered in an unexpected way. Try again; if it keeps happening, plex.tv may be having issues.',
  service_not_configured:
    'No Plex server is configured yet. An administrator must finish setup first.',
  already_initialized:
    'Setup is already complete. Change settings from the Settings page instead.',
  app_key_changed: 'The recovery key changed while you were rotating it. Refresh and try again.',
}

describe('AuthErrorCard', () => {
  it.each(Object.entries(COPY))(
    'renders the exact, actionable copy for %s',
    (code, message) => {
      render(<AuthErrorCard error={toApiError({ detail: code }, 400)} />)
      expect(screen.getByText(message)).toBeInTheDocument()
    },
  )

  it('maps a browser-side PlexPinError code through the same copy table', () => {
    render(<AuthErrorCard error={new PlexPinError('plex_popup_blocked')} />)
    expect(screen.getByText(COPY.plex_popup_blocked)).toBeInTheDocument()
  })

  it('renders the raw code (never a generic string) for an unknown detail code', () => {
    render(<AuthErrorCard error={toApiError({ detail: 'totally_unknown_code' }, 500)} />)
    // The raw code appears as the message AND in the technical details.
    expect(screen.getAllByText('totally_unknown_code').length).toBeGreaterThan(0)
  })

  it('renders the honest HTTP-status fallback (no bare catch-all) for a detail-less failure', () => {
    render(<AuthErrorCard error={toApiError({}, 503)} />)
    // The exact fallback is pinned positively, so the old generic sentence
    // cannot be reintroduced without failing this test.
    expect(
      screen.getByText('The server returned an unexpected error (HTTP 503).'),
    ).toBeInTheDocument()
  })

  it('renders the actionable hint when the envelope carries one', () => {
    const error = toApiError(
      { detail: 'server_identity_failed', hint: 'Double-check the port.' },
      502,
    )
    render(<AuthErrorCard error={error} />)
    expect(screen.getByText('Double-check the port.')).toBeInTheDocument()
  })

  it('shows the code and diagnostics key/values inside the Technical details expando', () => {
    const error = toApiError(
      {
        detail: 'server_unreachable_from_backend',
        diagnostics: { host: 'http://localhost:32400', reason: 'connection refused' },
      },
      502,
    )
    render(<AuthErrorCard error={error} />)

    const details = screen.getByText('Technical details').closest('details')
    expect(details).not.toBeNull()
    const expando = within(details as HTMLElement)
    expect(expando.getByText('server_unreachable_from_backend')).toBeInTheDocument()
    expect(expando.getByText('host')).toBeInTheDocument()
    expect(expando.getByText('http://localhost:32400')).toBeInTheDocument()
    expect(expando.getByText('reason')).toBeInTheDocument()
    expect(expando.getByText('connection refused')).toBeInTheDocument()
  })
})
