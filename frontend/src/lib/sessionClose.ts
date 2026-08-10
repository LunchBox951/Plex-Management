import { useSyncExternalStore } from 'react'

/**
 * Why the server ended the realtime stream — and, when that also ended the
 * session, the honest sentence to show the person it happened to.
 *
 * The backend sends one final SSE `closed` frame naming the reason before the
 * body terminates (`web/routers/events.py`). Without it every server-side close
 * looks identical to a dropped connection, so an operator whose sessions were
 * cut by the share-revalidation sweep would land on the sign-in screen with no
 * explanation at all — the gap issue #556 closes. Honesty over silence: an
 * unrecognized reason still produces a message that says a reason arrived and
 * names it, never a reassuring generic.
 */
export interface SessionCloseNotice {
  /** The raw reason string the server sent — kept for diagnostics/tests. */
  reason: string
  title: string
  message: string
  /** `true` when this close also ended the session, i.e. it explains a sign-out. */
  signedOut: boolean
}

interface NoticeCopy {
  title: string
  message: string
  signedOut: boolean
}

/**
 * The close-reason table. Each entry is the operator-facing wording ratified
 * with the sign-out design (#391): the two share-sweep causes get DIFFERENT
 * words on purpose, because a revoked share and a dead Plex token are different
 * facts and telling someone their access was removed when their password merely
 * changed would be a lie.
 */
const NOTICES: Record<string, NoticeCopy> = {
  share_revalidation_share_revoked: {
    title: 'Your Plex access was removed',
    message:
      'A re-check with Plex found this account no longer has access to the server, so it was signed out. Ask the server owner to re-share the libraries, then sign in again.',
    signedOut: true,
  },
  share_revalidation_token_stale: {
    title: 'Your Plex sign-in expired',
    message:
      'Plex no longer accepts this account’s saved sign-in — changing your Plex password does this — so access could not be re-checked and you were signed out. Your access to the server was NOT removed: sign in with Plex again to continue.',
    signedOut: true,
  },
  share_revalidation_signed_out: {
    title: 'You were signed out by a Plex re-check',
    message:
      'An automatic re-check with Plex ended this session. Sign in with Plex again; if it keeps happening, check Settings → Automatic sign-outs for the recorded reason.',
    signedOut: true,
  },
  session_logged_out: {
    title: 'You signed out',
    message: 'This session ended because it was signed out.',
    signedOut: true,
  },
  sessions_revoked: {
    title: 'An administrator signed this session out',
    message: 'Your sessions were revoked from the sessions page. You can sign in again with Plex.',
    signedOut: true,
  },
  // NOT a sign-out. A demotion closes the admin-only realtime stream and
  // nothing else — the session stays valid and keeps working with shared-user
  // access (`routers/auth.py` only calls `close_realtime_streams` here). Marking
  // it `signedOut` would also strand the wrong message forever: the reconnect
  // gets a 403 and returns outright, so the connect-time self-correcting clear
  // could never fire for that tab.
  permission_downgraded: {
    title: 'You are no longer an administrator',
    message:
      'This account’s administrator access on this server was removed, so its live admin connection was closed. You are still signed in with the access you now have.',
    signedOut: false,
  },
  session_expired: {
    title: 'Your session expired',
    message: 'Sessions end after a period of inactivity. Sign in with Plex again to continue.',
    signedOut: true,
  },
  plex_server_repointed: {
    title: 'The Plex server was changed',
    message:
      'This install was pointed at a different Plex server, so every session was ended. Sign in again against the new server.',
    signedOut: true,
  },
  app_key_rotated: {
    title: 'The access key was rotated',
    message: 'The shared API key changed, so this connection was closed. Reconnecting…',
    signedOut: false,
  },
  app_key_revoked: {
    title: 'The access key was revoked',
    message: 'The shared API key was removed, so this connection was closed.',
    signedOut: false,
  },
  // The single most common server-side close: every graceful shutdown/restart
  // ends every stream (`web/app.py`'s lifespan). Sessions survive a restart, so
  // this must never read as a sign-out — and without an entry here it would
  // land in the unknown-reason branch and quote the raw internal string.
  shutdown: {
    title: 'The server is restarting',
    message: 'Plex Manager closed its live connections while shutting down. Reconnecting…',
    signedOut: false,
  },
}

/**
 * Turn a server close reason into displayable copy.
 *
 * An unknown reason is NOT swallowed: it renders with the raw reason quoted, so
 * a backend that grows a new close cause degrades to "we don't have nice words
 * for this yet" rather than to silence.
 */
export function describeSessionClose(reason: string): SessionCloseNotice {
  const known = NOTICES[reason]
  if (known !== undefined) return { reason, ...known }
  return {
    reason,
    title: 'This session was ended by the server',
    message: `The server closed the live connection and gave the reason “${reason}”. Sign in again to continue.`,
    signedOut: true,
  }
}

/**
 * The most recent server-initiated close that ended the session, or `null`.
 *
 * A module-level store rather than React state because the realtime stream is
 * started outside the component tree (`RealtimeProvider` owns its lifetime) and
 * the screen that must show the message — the sign-in gate — only mounts AFTER
 * the session is already gone. Cleared on a successful sign-in so a stale
 * explanation never haunts the next session.
 */
let notice: SessionCloseNotice | null = null
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getSessionCloseNotice(): SessionCloseNotice | null {
  return notice
}

function emit(): void {
  listeners.forEach((listener) => listener())
}

/** Record a server close reason; non-sign-out reasons are deliberately ignored. */
export function noteSessionClose(reason: string): void {
  const described = describeSessionClose(reason)
  if (!described.signedOut) return
  notice = described
  emit()
}

export function clearSessionCloseNotice(): void {
  if (notice === null) return
  notice = null
  emit()
}

export function useSessionCloseNotice(): SessionCloseNotice | null {
  return useSyncExternalStore(subscribe, getSessionCloseNotice, getSessionCloseNotice)
}
