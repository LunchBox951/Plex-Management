import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { Fragment, useState, type ReactNode } from 'react'
import { cn } from '../../lib/cn'
import { useIsPhone } from '../../lib/useMediaQuery'
import { Sheet } from './Sheet'
import { MoreIcon } from './icons'

export interface ActionMenuItem {
  key: string
  label: string
  /** A second line explaining the effect ("Blocklist this release and find another"). */
  description?: string | undefined
  /** Red, below a divider: deletes, blocklists or affects other people. */
  danger?: boolean
  onSelect: () => void
}

interface ActionMenuProps {
  /** Accessible name of the ⋯ trigger, e.g. "Actions for Survivor". */
  label: string
  /** Heading of the phone action sheet (usually the title). */
  heading: ReactNode
  /** Only the actions legal for this row: hidden, never disabled. */
  items: ActionMenuItem[]
  className?: string
}

/**
 * The ⋯ menu: a dropdown on desktop, a bottom action sheet on phones. Safe
 * actions first, then a divider and the danger section. Selecting an item closes
 * the menu, then runs it (a danger item opens its ConfirmDialog).
 */
export function ActionMenu({ label, heading, items, className }: ActionMenuProps) {
  const isPhone = useIsPhone()
  const [sheetOpen, setSheetOpen] = useState(false)
  const safe = items.filter((item) => !item.danger)
  const danger = items.filter((item) => item.danger)
  if (items.length === 0) return null

  const trigger = (
    <button
      type="button"
      aria-label={label}
      className={cn(
        'flex size-8 items-center justify-center rounded-md text-muted outline-none',
        'hover:bg-white/10 hover:text-ink focus-visible:ring-2 focus-visible:ring-white/40',
        'data-[state=open]:bg-white/10 data-[state=open]:text-ink',
        className,
      )}
      onClick={isPhone ? () => setSheetOpen(true) : undefined}
    >
      <MoreIcon />
    </button>
  )

  if (isPhone) {
    return (
      <>
        {trigger}
        <Sheet open={sheetOpen} onOpenChange={setSheetOpen} title={heading} size="sm">
          <div className="-mx-5 flex flex-col">
            {[safe, danger].map((group, groupIndex) =>
              group.length === 0 ? null : (
                <Fragment key={groupIndex}>
                  {groupIndex === 1 && safe.length > 0 ? (
                    <div className="mx-5 my-1 h-px bg-hairline" />
                  ) : null}
                  {group.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => {
                        setSheetOpen(false)
                        item.onSelect()
                      }}
                      className={cn(
                        'flex min-h-12 flex-col justify-center px-5 py-3 text-left text-[15px] font-medium',
                        'outline-none hover:bg-white/6 focus-visible:bg-white/8',
                        item.danger ? 'text-error' : 'text-ink',
                      )}
                    >
                      {item.label}
                      {item.description ? (
                        <span className="text-[12.5px] font-normal text-faint">{item.description}</span>
                      ) : null}
                    </button>
                  ))}
                </Fragment>
              ),
            )}
          </div>
        </Sheet>
      </>
    )
  }

  return (
    <DropdownMenu.Root modal={false}>
      <DropdownMenu.Trigger asChild>{trigger}</DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-50 w-64 rounded-[10px] bg-surface p-1.5 shadow-[0_12px_40px_rgb(0_0_0/60%)] ring-1 ring-white/10"
        >
          {[safe, danger].map((group, groupIndex) =>
            group.length === 0 ? null : (
              <Fragment key={groupIndex}>
                {groupIndex === 1 && safe.length > 0 ? (
                  <DropdownMenu.Separator className="my-1 h-px bg-hairline" />
                ) : null}
                {group.map((item) => (
                  <DropdownMenu.Item
                    key={item.key}
                    onSelect={item.onSelect}
                    className={cn(
                      'flex cursor-pointer flex-col rounded-md px-2.5 py-2 text-[13.5px] leading-[18px] font-medium outline-none',
                      'data-[highlighted]:bg-white/6',
                      item.danger ? 'text-error' : 'text-ink',
                    )}
                  >
                    {item.label}
                    {item.description ? (
                      <span className="text-[11.5px] leading-[15px] font-normal text-faint">
                        {item.description}
                      </span>
                    ) : null}
                  </DropdownMenu.Item>
                ))}
              </Fragment>
            ),
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}
