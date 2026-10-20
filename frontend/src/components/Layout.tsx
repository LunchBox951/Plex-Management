import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { useState, type ReactNode } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router'
import { useAuthMe, useLogout, useRequests } from '../api/hooks'
import { cn } from '../lib/cn'
import { isInFlightRequestStatus, needsAttention } from '../lib/status'
import { Logo } from './brand/Logo'
import { HealthDot } from './HealthDot'
import { NotificationBell } from './notifications/NotificationBell'
import { useUnreadNotificationCount } from './notifications/useUnreadNotificationCount'
import { SearchOverlay } from './SearchOverlay'
import { CountBadge } from './ui/CountBadge'
import {
  BellIcon,
  BookmarkIcon,
  HomeIcon,
  ManageIcon,
  SearchIcon,
  SignOutIcon,
} from './ui/icons'

const WATCH_NAV = [
  { to: '/', label: 'Home', end: true },
  { to: '/movies', label: 'Movies', end: false },
  { to: '/tv', label: 'TV', end: false },
  { to: '/requests', label: 'My requests', end: false },
] as const

/** Manage mode's tabs, in the order the design system lists them. */
const MANAGE_TABS = [
  // Phones land on Overview (desktop has no Overview tab: its header holds the same facts).
  { to: '/manage/overview', label: 'Overview', phoneOnly: true },
  { to: '/manage/attention', label: 'Needs attention' },
  { to: '/manage/requests', label: 'Requests' },
  { to: '/manage/downloads', label: 'Downloads' },
  { to: '/manage/blocklist', label: 'Blocklist' },
  { to: '/manage/quality', label: 'Quality' },
  { to: '/manage/settings', label: 'Settings' },
  { to: '/manage/logs', label: 'Logs' },
] as const

function initials(name: string | undefined): string {
  if (!name) return '·'
  const parts = name.trim().split(/[\s._-]+/).filter(Boolean)
  return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase() || '·'
}

function ProfileMenu({ name, avatarUrl }: { name: string | undefined; avatarUrl?: string | null }) {
  const logout = useLogout()
  return (
    <DropdownMenu.Root modal={false}>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="Profile"
          className="flex size-[30px] shrink-0 items-center justify-center overflow-hidden rounded-full bg-surface text-xs font-semibold text-muted ring-1 ring-white/10 outline-none hover:ring-white/25 focus-visible:ring-2 focus-visible:ring-white/40"
        >
          {avatarUrl ? <img src={avatarUrl} alt="" className="size-full object-cover" /> : initials(name)}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-50 w-56 rounded-[10px] bg-surface p-1.5 shadow-[0_12px_40px_rgb(0_0_0/60%)] ring-1 ring-white/10"
        >
          <div className="px-2.5 pt-1.5 pb-2 text-[13px] font-semibold text-ink">{name ?? 'Signed in'}</div>
          <DropdownMenu.Separator className="my-1 h-px bg-hairline" />
          <DropdownMenu.Item
            onSelect={() => void logout.mutateAsync()}
            className="flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-2 text-[13.5px] font-medium text-ink outline-none data-[highlighted]:bg-white/6"
          >
            <SignOutIcon className="size-4 text-muted" />
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}

function TabBarItem({
  to,
  end,
  label,
  icon,
  badge,
  onClick,
  activeTone = 'ink',
}: {
  to?: string
  end?: boolean
  label: string
  icon: ReactNode
  badge?: ReactNode
  onClick?: () => void
  activeTone?: 'ink' | 'gold'
}) {
  const content = (active: boolean) => (
    <>
      <span className="relative">
        {icon}
        {badge ? <span className="absolute -top-2 left-3">{badge}</span> : null}
      </span>
      <span className="whitespace-nowrap">{label}</span>
      {active ? <span className="sr-only">(current)</span> : null}
    </>
  )
  const base =
    'flex min-h-11 flex-1 flex-col items-center justify-start gap-1 pt-1 text-[10.5px] font-semibold outline-none focus-visible:text-ink'
  if (!to) {
    return (
      <button type="button" onClick={onClick} className={cn(base, 'text-faint')}>
        {content(false)}
      </button>
    )
  }
  return (
    <NavLink
      to={to}
      end={end ?? false}
      className={({ isActive }) =>
        cn(base, isActive ? (activeTone === 'gold' ? 'text-gold' : 'text-ink') : 'text-faint')
      }
    >
      {({ isActive }) => content(isActive)}
    </NavLink>
  )
}

/**
 * The app shell. Desktop: one header (logo, Home · Movies · TV · My requests,
 * search, bell, Manage for admins, profile). Manage mode adds a row of tabs under
 * the header. Phones: a slim top bar and a bottom tab bar (Home, Search, My
 * requests, Notifications, plus Manage for admins).
 */
export function Layout() {
  const auth = useAuthMe()
  const requests = useRequests({ poll: true })
  const location = useLocation()
  const isAdmin = auth.data?.is_admin ?? auth.data?.user?.is_admin ?? false
  const [searchOpen, setSearchOpen] = useState(false)
  const unreadNotifications = useUnreadNotificationCount()
  const inManage = location.pathname === '/manage' || location.pathname.startsWith('/manage/')
  // Only a settled `/requests` payload feeds the counts: while the query is
  // unresolved (or has never succeeded) they stay `undefined` and the badges are
  // hidden — never an optimistic guess. A transient poll error keeps the last
  // good `data`, so a count holds rather than flashing to nothing.
  // "My requests" counts the viewer's own (or joined) in-flight requests: an
  // admin's list holds everyone's, which Manage's counts cover instead.
  const inFlightRequestCount = requests.data
    ? requests.data.requests.filter(
        (request) =>
          (request.is_owner || request.can_withdraw) && isInFlightRequestStatus(request.status),
      ).length
    : undefined
  const attentionCount =
    isAdmin && requests.data
      ? requests.data.requests.filter((request) => needsAttention(request.status)).length
      : 0
  const userName = auth.data?.user?.username

  return (
    <div className="min-h-screen bg-bg text-ink">
      <header className="sticky top-0 z-40 border-b border-hairline bg-bg/85 backdrop-blur">
        <div className="flex h-14 w-full items-center gap-3 px-4 sm:gap-6 sm:px-8">
          <Link
            to="/"
            aria-label="Plex Manager home"
            className="shrink-0 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-white/40"
          >
            <Logo />
          </Link>
          <nav aria-label="Main" className="hidden min-w-0 items-center gap-1 overflow-x-auto sm:flex">
            {WATCH_NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'flex h-[30px] shrink-0 items-center gap-1.5 rounded-full px-3 text-[13px] font-semibold transition-colors outline-none focus-visible:ring-2 focus-visible:ring-white/40',
                    isActive && !inManage ? 'bg-white/8 text-ink' : 'text-faint hover:text-ink',
                  )
                }
              >
                {item.label}
                {item.to === '/requests' && inFlightRequestCount ? (
                  <CountBadge count={inFlightRequestCount} label="active requests" />
                ) : null}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-2 sm:gap-3">
            {/* One overlay instance: its field trigger shows from md up; the
                sm–md icon below and the phone Search tab open the same dialog
                through `searchOpen` (its portal ignores the wrapper's display). */}
            <span className="hidden md:block">
              <SearchOverlay open={searchOpen} onOpenChange={setSearchOpen} />
            </span>
            <button
              type="button"
              aria-label="Search movies and shows"
              onClick={() => setSearchOpen(true)}
              className="hidden size-9 items-center justify-center rounded-full text-muted outline-none hover:bg-white/8 hover:text-ink focus-visible:ring-2 focus-visible:ring-white/40 sm:flex md:hidden"
            >
              <SearchIcon />
            </button>
            <span className="hidden sm:block">
              <NotificationBell />
            </span>
            {isAdmin ? (
              <Link
                to="/manage"
                aria-current={inManage ? 'page' : undefined}
                className={cn(
                  'hidden h-8 items-center gap-1.5 rounded-full px-3 text-[12.5px] font-semibold outline-none sm:inline-flex',
                  'focus-visible:ring-2 focus-visible:ring-gold/60',
                  inManage
                    ? 'bg-gold text-gold-ink'
                    : 'text-gold ring-1 ring-inset ring-gold/35 hover:bg-gold/10',
                )}
              >
                Manage
                <CountBadge count={attentionCount} tone="error" label="need attention" />
              </Link>
            ) : null}
            <ProfileMenu name={userName} avatarUrl={auth.data?.user?.avatar_url ?? null} />
          </div>
        </div>
        {isAdmin && inManage ? (
          <nav
            aria-label="Manage"
            className="flex items-center gap-0.5 overflow-x-auto border-t border-hairline bg-surface-deep px-2 sm:px-6"
          >
            {MANAGE_TABS.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                className={({ isActive }) =>
                  cn(
                    'phoneOnly' in tab && 'sm:hidden',
                    'flex h-11 shrink-0 items-center gap-1.5 border-b-2 px-3 text-[13px] font-semibold outline-none focus-visible:text-ink',
                    isActive
                      ? 'border-gold text-ink'
                      : 'border-transparent text-faint hover:text-ink',
                  )
                }
              >
                {tab.label}
                {tab.to === '/manage/attention' ? (
                  <CountBadge count={attentionCount} tone="error" label="need attention" />
                ) : null}
              </NavLink>
            ))}
            <NavLink
              to="/manage/health"
              className={({ isActive }) =>
                cn(
                  'ml-auto flex h-11 shrink-0 items-center gap-2 border-b-2 px-3 text-[13px] font-semibold outline-none',
                  isActive ? 'border-gold text-ink' : 'border-transparent text-faint hover:text-ink',
                )
              }
            >
              System health
              <HealthDot />
            </NavLink>
          </nav>
        ) : null}
      </header>

      <main className="pb-[calc(84px+env(safe-area-inset-bottom))] sm:pb-0">
        <Outlet context={{ searchOpen }} />
      </main>

      <nav
        aria-label="Tabs"
        className="fixed inset-x-0 bottom-0 z-40 flex border-t border-hairline bg-bg/92 px-1 pt-2.5 pb-[max(1.25rem,env(safe-area-inset-bottom))] backdrop-blur sm:hidden"
      >
        <TabBarItem to="/" end label="Home" icon={<HomeIcon />} />
        <TabBarItem label="Search" icon={<SearchIcon />} onClick={() => setSearchOpen(true)} />
        <TabBarItem
          to="/requests"
          label="My requests"
          icon={<BookmarkIcon />}
          badge={
            inFlightRequestCount ? (
              <CountBadge count={inFlightRequestCount} label="active requests" className="h-4 min-w-4 px-1 text-[10px]" />
            ) : null
          }
        />
        <TabBarItem
          to="/notifications"
          label="Notifications"
          icon={<BellIcon />}
          badge={
            <CountBadge count={unreadNotifications} label="unread notifications" className="h-4 min-w-4 px-1 text-[10px]" />
          }
        />
        {isAdmin ? (
          <TabBarItem
            to="/manage"
            label="Manage"
            activeTone="gold"
            icon={<ManageIcon />}
            badge={
              <CountBadge count={attentionCount} tone="error" label="need attention" className="h-4 min-w-4 px-1 text-[10px]" />
            }
          />
        ) : null}
      </nav>
    </div>
  )
}
