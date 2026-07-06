import { NavLink, Outlet } from 'react-router-dom'
import { useAuthMe, useLogout } from '../api/hooks'
import { cn } from '../lib/cn'
import { HealthDot } from './HealthDot'
import { Button } from './ui/Button'

const NAV = [
  { to: '/', label: 'Discover', end: true, admin: false },
  { to: '/requests', label: 'Requests', end: false, admin: false },
  { to: '/queue', label: 'Queue', end: false, admin: true },
  { to: '/status', label: 'Status', end: false, admin: true },
  { to: '/logs', label: 'Logs', end: false, admin: true },
  { to: '/settings', label: 'Settings', end: false, admin: true },
  { to: '/blocklist', label: 'Blocklist', end: false, admin: true },
]

export function Layout() {
  const logout = useLogout()
  const auth = useAuthMe()
  const isAdmin = auth.data?.is_admin ?? auth.data?.user?.is_admin ?? false
  const nav = NAV.filter((item) => !item.admin || isAdmin)

  return (
    <div className="min-h-screen bg-bg text-ink">
      <header className="sticky top-0 z-40 border-b border-hairline bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-5">
          <NavLink
            to="/"
            className="shrink-0 font-display text-[17px] font-extrabold tracking-wide"
          >
            PLEX<span className="text-gold">MGR</span>
          </NavLink>
          <nav className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'rounded-md px-3 py-1.5 text-[13px] font-semibold transition-colors',
                    isActive ? 'bg-white/8 text-ink' : 'text-faint hover:text-ink',
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-3">
            <HealthDot />
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
      <main className="mx-auto max-w-6xl px-5 py-8">
        <Outlet />
      </main>
    </div>
  )
}
