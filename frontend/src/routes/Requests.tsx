import { useState, type ReactNode } from 'react'
import { useCancelRequest, useRequests, useWithdrawSubscription } from '../api/hooks'
import type { RequestResponse } from '../api/types'
import { ProblemCard } from '../components/requests/ProblemCard'
import { RequestRow } from '../components/requests/RequestRow'
import { useRequestDialog } from '../components/requests/useRequestDialog'
import { Button } from '../components/ui/Button'
import { ConfirmDialog } from '../components/ui/ConfirmDialog'
import { LinkButton } from '../components/ui/LinkButton'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'
import { cn } from '../lib/cn'
import { onTheWayLine } from '../lib/requestCopy'
import { needsAttention } from '../lib/status'

/** Requests the viewer asked for or joined. Admins manage everyone's in Manage. */
function isMine(request: RequestResponse): boolean {
  return request.is_owner || request.can_withdraw
}

const DONE = new Set(['available', 'partially_available', 'failed', 'cancelled', 'evicted'])
/** Not-yet-imported statuses the cancel verb accepts (ADR-0014). */
const CANCELLABLE = new Set(['pending', 'searching', 'no_acceptable_release', 'downloading'])

function Group({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5">
      <h2 className="pt-2 font-mono text-[10.5px] leading-none font-semibold tracking-[0.14em] text-faint uppercase">
        {label}
      </h2>
      {children}
    </section>
  )
}

/**
 * My requests: the viewer's own requests, grouped and in plain words. Stuck
 * ones explain themselves (ProblemCard) and offer Let the admin know; the rest
 * read as "On the way". Done holds what arrived or was settled. Clicking a
 * request opens its title.
 */
export function Requests() {
  const { data, isLoading, isError, error, refetch } = useRequests({ poll: true })
  const [view, setView] = useState<'active' | 'done'>('active')
  const { openRequest, dialog } = useRequestDialog()
  const { toast } = useToast()
  const cancel = useCancelRequest()
  const withdraw = useWithdrawSubscription()
  const [confirmCancel, setConfirmCancel] = useState<RequestResponse | null>(null)

  const mine = (data?.requests ?? []).filter(isMine)
  const active = mine.filter((request) => !DONE.has(request.status))
  const done = mine.filter((request) => DONE.has(request.status))
  const stuck = active.filter((request) => needsAttention(request.status))
  const onTheWay = active.filter((request) => !needsAttention(request.status))

  const withdrawFrom = (request: RequestResponse) => {
    withdraw.mutate(request.id, {
      onSuccess: () => toast({ title: `Withdrew from ${request.title}`, intent: 'success' }),
      onError: (err) => toast({ title: "Couldn't withdraw", description: err.message, intent: 'error' }),
    })
  }

  const leaveAction = (request: RequestResponse): ReactNode => {
    if (request.can_withdraw && request.has_other_participants && !request.is_owner) {
      return (
        <Button variant="ghost" loading={withdraw.isPending} onClick={() => withdrawFrom(request)}>
          Withdraw
        </Button>
      )
    }
    if (request.can_mutate && CANCELLABLE.has(request.status)) {
      return (
        <Button variant="ghost" onClick={() => setConfirmCancel(request)}>
          Cancel my request
        </Button>
      )
    }
    return null
  }

  let body: ReactNode
  if (isLoading) {
    body = <CenteredSpinner label="Loading your requests…" />
  } else if (isError && !data) {
    body = (
      <StateMessage
        tone="error"
        title="Couldn't load your requests"
        message={error.message}
        action={
          <Button onClick={() => void refetch()} variant="secondary">
            Retry
          </Button>
        }
      />
    )
  } else if ((view === 'active' ? active : done).length === 0) {
    body =
      view === 'active' ? (
        <StateMessage
          title="Nothing on the way"
          message="Find something to watch and press Request. It'll show up here while it's on its way."
          action={<LinkButton to="/">Browse what's trending</LinkButton>}
        />
      ) : (
        <StateMessage title="Nothing here yet" message="Requests that arrive or finish show up here." />
      )
  } else if (view === 'done') {
    body = (
      <ul className="flex flex-col gap-3">
        {done.map((request) => (
          <RequestRow key={request.id} request={request} onOpen={() => openRequest(request)} onReSearch={null} />
        ))}
      </ul>
    )
  } else {
    body = (
      <div className="flex flex-col gap-6">
        {stuck.length > 0 ? (
          <Group label="Needs the admin">
            {stuck.map((request) => (
              <ProblemCard
                key={request.id}
                request={request}
                audience="family"
                onOpen={() => openRequest(request)}
                actions={leaveAction(request)}
              />
            ))}
          </Group>
        ) : null}
        {onTheWay.length > 0 ? (
          <Group label="On the way">
            <ul className="flex flex-col gap-3">
              {onTheWay.map((request) => (
                <RequestRow
                  key={request.id}
                  request={request}
                  line={onTheWayLine(request)}
                  onOpen={() => openRequest(request)}
                  onReSearch={null}
                />
              ))}
            </ul>
          </Group>
        ) : null}
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-[860px] px-4 py-6 sm:px-8 sm:py-8">
      <header className="mb-4 flex items-baseline gap-3">
        <h1 className="font-display text-[28px] leading-tight font-extrabold sm:text-2xl">My requests</h1>
        {data ? <span className="font-mono text-sm text-muted">{active.length} active</span> : null}
      </header>
      <div role="tablist" aria-label="Which requests" className="mb-5 flex rounded-[10px] bg-surface p-[3px] sm:max-w-xs">
        {(['active', 'done'] as const).map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={view === key}
            onClick={() => setView(key)}
            className={cn(
              'h-[34px] flex-1 rounded-lg text-[13px] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-white/40',
              view === key ? 'bg-white/10 text-ink' : 'text-faint hover:text-ink',
            )}
          >
            {key === 'active' ? 'Active' : 'Done'}
          </button>
        ))}
      </div>
      {body}
      {dialog}
      <ConfirmDialog
        open={confirmCancel !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmCancel(null)
        }}
        title={confirmCancel ? `Cancel ${confirmCancel.title}?` : 'Cancel request?'}
        intro="This stops your request."
        effects={[
          { tone: 'risk', text: 'Any download in progress stops and its files are deleted.' },
          { tone: 'info', text: 'Anything already in Plex stays where it is.' },
          { tone: 'info', text: 'You can request it again later.' },
        ]}
        confirmLabel="Cancel request"
        cancelLabel="Keep request"
        pending={cancel.isPending}
        error={cancel.isError ? cancel.error.message : null}
        onConfirm={() => {
          if (!confirmCancel) return
          const title = confirmCancel.title
          cancel.mutate(confirmCancel.id, {
            onSuccess: () => {
              setConfirmCancel(null)
              toast({ title: `Cancelled ${title}`, intent: 'success' })
            },
          })
        }}
      />
    </div>
  )
}
