import { NavLink, Outlet } from 'react-router-dom'
import { cn } from '../lib/cn'
import { HealthDot } from './HealthDot'

const NAV = [
  { to: '/', label: 'Discover', end: true },
  { to: '/requests', label: 'Requests', end: false },
  { to: '/queue', label: 'Queue', end: false },
  { to: '/status', label: 'Status', end: false },
  { to: '/logs', label: 'Logs', end: false },
  { to: '/settings', label: 'Settings', end: false },
  { to: '/blocklist', label: 'Blocklist', end: false },
]

export function Layout() {
  return (
    <div className="min-h-screen bg-bg text-ink">
      <header className="sticky top-0 z-40 border-b border-hairline bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-5">
          <NavLink to="/" className="font-display text-[17px] font-extrabold tracking-wide">
            PLEX<span className="text-gold">MGR</span>
          </NavLink>
          <nav className="flex items-center gap-1">
            {NAV.map((item) => (
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
          <div className="ml-auto">
            <HealthDot />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 py-8">
        <Outlet />
      </main>
    </div>
  )
}
