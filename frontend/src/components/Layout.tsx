import { Link, NavLink, Outlet } from 'react-router-dom'
import { useAuthMe, useLogout, useRequests } from '../api/hooks'
import { cn } from '../lib/cn'
import { HealthDot } from './HealthDot'
import { Button } from './ui/Button'

const USER_NAV = [
  { to: '/', label: 'Discover', end: true },
  { to: '/requests', label: 'Requests', end: false },
] as const

const ADMIN_NAV = [
  { to: '/queue', label: 'Queue', end: false },
  { to: '/status', label: 'Status', end: false },
  { to: '/logs', label: 'Logs', end: false },
  { to: '/settings', label: 'Settings', end: false },
  { to: '/blocklist', label: 'Blocklist', end: false },
] as const

const REQUEST_NAV_BADGE_STATUSES = new Set([
  'searching',
  'downloading',
  'no_acceptable_release',
])

export function Layout() {
  const logout = useLogout()
  const auth = useAuthMe()
  const requests = useRequests({ poll: true })
  const isAdmin = auth.data?.is_admin ?? auth.data?.user?.is_admin ?? false
  const requestAttentionCount = requests.data
    ? requests.data.requests.filter((request) => REQUEST_NAV_BADGE_STATUSES.has(request.status))
        .length
    : undefined

  return (
    <div className="min-h-screen bg-bg text-ink">
      <header className="sticky top-0 z-40 border-b border-hairline bg-bg/85 backdrop-blur">
        <div className="flex h-14 w-full items-center gap-3 px-5 sm:gap-6 sm:px-8">
          <NavLink
            to="/"
            className="shrink-0 font-display text-[17px] font-extrabold tracking-wide"
          >
            PLEX<span className="text-gold">MGR</span>
          </NavLink>
          <div className="flex min-w-0 flex-1 items-center overflow-x-auto px-0.5 py-1">
            <nav aria-label="User" className="flex shrink-0 items-center gap-1">
              {USER_NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    cn(
                      'flex h-7 shrink-0 items-center gap-1.5 rounded-full px-3 font-sans text-[13px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/40',
                      isActive ? 'bg-white/8 text-ink' : 'text-faint hover:text-ink',
                    )
                  }
                >
                  <span>{item.label}</span>
                  {item.to === '/requests' && requestAttentionCount ? (
                    <span className="inline-flex min-w-4.5 items-center justify-center rounded-full bg-gold px-1.5 py-0.5 font-mono text-[10px] leading-none font-semibold text-gold-ink tabular-nums">
                      <span aria-hidden>{requestAttentionCount}</span>
                      <span className="sr-only">
                        {`, ${requestAttentionCount} `}
                        {requestAttentionCount === 1 ? 'active request' : 'active requests'}
                      </span>
                    </span>
                  ) : null}
                </NavLink>
              ))}
            </nav>
            {isAdmin ? (
              <>
                <span
                  role="separator"
                  aria-orientation="vertical"
                  className="mx-2.5 h-[22px] w-px shrink-0 bg-white/10"
                />
                <nav aria-label="Administration" className="flex shrink-0 items-center gap-0.5">
                  {ADMIN_NAV.map((item) => (
                    <NavLink
                      key={item.to}
                      to={item.to}
                      end={item.end}
                      className={({ isActive }) =>
                        cn(
                          'flex h-6 shrink-0 items-center rounded-full px-2.5 font-mono text-[11.5px] font-medium tracking-[.02em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60',
                          isActive ? 'bg-gold/12 text-gold' : 'text-faint hover:text-gold',
                        )
                      }
                    >
                      {item.label}
                    </NavLink>
                  ))}
                </nav>
              </>
            ) : null}
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-3">
            {isAdmin ? (
              <Link
                to="/status"
                title="Open system status"
                className="inline-flex min-h-6 items-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60 focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
              >
                <span className="sr-only">Open system status: </span>
                <HealthDot />
              </Link>
            ) : (
              <HealthDot />
            )}
            <Button
              size="sm"
              variant="ghost"
              loading={logout.isPending}
              onClick={() => void logout.mutateAsync()}
            >
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  )
}
