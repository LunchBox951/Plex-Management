import type { ReactNode, SVGProps } from 'react'
import { cn } from '../../lib/cn'

/**
 * Simple 24px line icons: 1.75px stroke, round caps, drawn in the text color
 * they sit beside. Decorative by default (`aria-hidden`); the control that holds
 * one carries the accessible name.
 */
function Icon({ className, children, ...props }: SVGProps<SVGSVGElement> & { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className={cn('size-[18px] shrink-0', className)}
      {...props}
    >
      {children}
    </svg>
  )
}

type IconProps = SVGProps<SVGSVGElement>

export const HomeIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" />
  </Icon>
)
export const SearchIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="M20 20l-4-4" />
  </Icon>
)
export const BookmarkIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 3h12v18l-6-4-6 4z" />
  </Icon>
)
export const BellIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z" />
    <path d="M10 20a2 2 0 0 0 4 0" />
  </Icon>
)
export const ManageIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1" />
  </Icon>
)
export const FilmIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4" />
  </Icon>
)
export const TvIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="6" width="18" height="12" rx="2" />
    <path d="M8 21h8M9 3l3 3 3-3" />
  </Icon>
)
export const PlayIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M7 5l12 7-12 7z" fill="currentColor" />
  </Icon>
)
export const PlusIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 5v14M5 12h14" />
  </Icon>
)
export const MoreIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="5" cy="12" r="1" fill="currentColor" />
    <circle cx="12" cy="12" r="1" fill="currentColor" />
    <circle cx="19" cy="12" r="1" fill="currentColor" />
  </Icon>
)
export const CheckIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M5 12.5l4.5 4.5L19 7.5" />
  </Icon>
)
export const CloseIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 6l12 12M18 6L6 18" />
  </Icon>
)
export const ChevronLeftIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M15 5l-7 7 7 7" />
  </Icon>
)
export const ChevronRightIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9 5l7 7-7 7" />
  </Icon>
)
export const AlertIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7.5v5.5M12 16.5v.01" />
  </Icon>
)
export const InfoIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5.5M12 7.5v.01" />
  </Icon>
)
export const UndoIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9 14L4 9l5-5" />
    <path d="M4 9h10a6 6 0 0 1 0 12h-3" />
  </Icon>
)
export const SignOutIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l5-5-5-5M15 12H4" />
  </Icon>
)
