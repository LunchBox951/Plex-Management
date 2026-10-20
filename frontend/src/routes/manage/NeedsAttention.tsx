import type { ReactNode } from 'react'
import { useRequests } from '../../api/hooks'
import type { RequestResponse } from '../../api/types'
import { useRequestActions } from '../../components/manage/useRequestActions'
import { ProblemCard } from '../../components/requests/ProblemCard'
import { useRequestDialog } from '../../components/requests/useRequestDialog'
import { AdminPageHeader } from '../../components/ui/AdminPageHeader'
import { Button } from '../../components/ui/Button'
import { CenteredSpinner, StateMessage } from '../../components/ui/feedback'
import { CheckIcon } from '../../components/ui/icons'
import { needsAttention } from '../../lib/status'

/** The stuck requests, each as an admin ProblemCard with its likely fixes. */
export function AttentionList({ limit }: { limit?: number }) {
  const { data, isLoading, isError, error, refetch } = useRequests({ poll: true })
  const { openRequest, pickRelease, dialog } = useRequestDialog()
  const actions = useRequestActions({ openRequest, pickRelease })
  const stuck = (data?.requests ?? []).filter((request) => needsAttention(request.status))
  const shown = limit != null ? stuck.slice(0, limit) : stuck

  const fixes = (request: RequestResponse): ReactNode => {
    const download = actions.downloadFor(request)
    if (request.status === 'import_blocked') {
      return (
        <>
          {download ? (
            <Button onClick={() => actions.confirmMarkBad(request)}>Find a different copy</Button>
          ) : null}
          {download ? (
            <Button variant="danger" onClick={() => actions.confirmImportAnyway(request)}>
              Import anyway…
            </Button>
          ) : null}
          <Button variant="secondary" onClick={() => openRequest(request)}>
            Review
          </Button>
        </>
      )
    }
    return (
      <>
        <Button onClick={() => pickRelease(request)}>Pick a release</Button>
        {actions.canSearchAgain(request) ? (
          <Button variant="secondary" onClick={() => actions.searchAgain(request)}>
            Search again
          </Button>
        ) : null}
      </>
    )
  }

  let body: ReactNode
  if (isLoading) body = <CenteredSpinner label="Checking for problems…" />
  else if (isError && !data)
    body = (
      <StateMessage
        tone="error"
        title="Couldn't load requests"
        message={error.message}
        action={
          <Button variant="secondary" onClick={() => void refetch()}>
            Retry
          </Button>
        }
      />
    )
  else if (stuck.length === 0)
    body = (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-white/10 px-6 py-12 text-center">
        <span className="flex size-9 items-center justify-center rounded-full bg-available/15 text-available">
          <CheckIcon />
        </span>
        <p className="font-display text-lg font-semibold">Nothing needs you</p>
        <p className="max-w-sm text-sm text-muted">Stuck downloads and failed searches show up here with a fix.</p>
      </div>
    )
  else
    body = (
      <div className="grid gap-3 lg:grid-cols-2">
        {shown.map((request) => (
          <ProblemCard
            key={request.id}
            request={request}
            audience="admin"
            download={actions.downloadFor(request)}
            subtitle={request.is_owner ? 'Asked by you' : 'Asked by someone else'}
            onOpen={() => openRequest(request)}
            actions={fixes(request)}
          />
        ))}
      </div>
    )

  return (
    <>
      {body}
      {dialog}
      {actions.dialogs}
    </>
  )
}

/** Manage › Needs attention. */
export function NeedsAttention() {
  return (
    <div className="mx-auto w-full max-w-[1180px] px-4 py-6 sm:px-8">
      <AdminPageHeader
        title="Needs attention"
        description="Requests that are stuck and waiting on you, each with its most likely fix."
        className="mb-5"
      />
      <AttentionList />
    </div>
  )
}
