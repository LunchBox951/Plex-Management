import { useCallback, useEffect, useRef, useState } from 'react'
import { useCreateRequest, useGrab, useSearchPreview } from '../api/hooks'
import type {
  AcceptedRelease,
  DiscoverResult,
  GrabRequest,
  SearchPreviewRequest,
  SearchPreviewResponse,
} from '../api/types'
import type { ApiError } from '../lib/errors'
import { Dialog } from './ui/Dialog'
import { ReleaseList } from './ReleaseList'
import { Button } from './ui/Button'
import { CenteredSpinner } from './ui/feedback'
import { useToast } from './ui/toast'

interface TitleDetailModalProps {
  title: DiscoverResult | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

function asApiError(error: unknown): ApiError {
  return error as ApiError
}

/**
 * The headline flow: request a title, run the decision engine (search-preview),
 * and grab a ranked release into the download client. This single modal exercises
 * the whole request -> search -> grab path the alpha exists to prove.
 */
export function TitleDetailModal({ title, open, onOpenChange }: TitleDetailModalProps) {
  const { toast } = useToast()
  const createRequest = useCreateRequest()
  const searchPreview = useSearchPreview()
  const grab = useGrab()

  const [requestId, setRequestId] = useState<number | null>(null)
  const [preview, setPreview] = useState<SearchPreviewResponse | null>(null)
  const [grabbingGuid, setGrabbingGuid] = useState<string | null>(null)

  // Reset the per-title flow whenever a different title is opened. Keyed on
  // media_type AND tmdb_id: TMDB movie/tv ids are independent namespaces and
  // collide, so tmdb_id alone would carry one title's request state onto another.
  const titleKey = title ? `${title.media_type}:${title.tmdb_id}` : null
  // Always-current title key, read by async handlers after an await to discard
  // results that belong to a title the modal has since moved on from.
  const latestTitleKey = useRef(titleKey)
  latestTitleKey.current = titleKey
  useEffect(() => {
    setRequestId(null)
    setPreview(null)
    setGrabbingGuid(null)
  }, [titleKey])

  const runPreview = useCallback(
    async (forRequestId: number | null) => {
      if (!title) return
      const startedKey = `${title.media_type}:${title.tmdb_id}`
      const body: SearchPreviewRequest =
        forRequestId !== null
          ? { request_id: forRequestId }
          : { tmdb_id: title.tmdb_id, media_type: title.media_type, title: title.title }
      if (forRequestId === null && typeof title.year === 'number') {
        body.year = title.year
      }
      try {
        const result = await searchPreview.mutateAsync(body)
        if (latestTitleKey.current !== startedKey) return // modal moved to another title
        setPreview(result)
      } catch (error) {
        if (latestTitleKey.current !== startedKey) return
        toast({ title: 'Search failed', description: asApiError(error).message, intent: 'error' })
      }
    },
    [title, searchPreview, toast],
  )

  const onRequest = useCallback(async () => {
    if (!title) return
    const startedKey = `${title.media_type}:${title.tmdb_id}`
    const titleName = title.title
    try {
      const created = await createRequest.mutateAsync({
        tmdb_id: title.tmdb_id,
        media_type: title.media_type,
      })
      if (latestTitleKey.current !== startedKey) return // don't apply A's request to title B
      setRequestId(created.id)
      toast({ title: `Requested ${titleName}`, intent: 'success' })
      await runPreview(created.id)
    } catch (error) {
      if (latestTitleKey.current !== startedKey) return
      toast({ title: 'Request failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [title, createRequest, toast, runPreview])

  const onGrab = useCallback(
    async (release: AcceptedRelease) => {
      // Need a request, and never fire a second grab while one is in flight.
      if (requestId === null || grab.isPending) return
      // Send only the GUID — it uniquely identifies the clicked row. info_hash can
      // be shared across indexers and the backend matches it BEFORE guid, so
      // including it could grab a different release that shares the hash.
      const body: GrabRequest = { request_id: requestId, guid: release.guid }
      setGrabbingGuid(release.guid)
      try {
        await grab.mutateAsync(body)
        toast({
          title: 'Grabbing release',
          description: 'Track its progress on the Queue screen.',
          intent: 'success',
        })
      } catch (error) {
        toast({ title: 'Grab failed', description: asApiError(error).message, intent: 'error' })
      } finally {
        setGrabbingGuid(null)
      }
    },
    [requestId, grab, toast],
  )

  if (!title) return null

  const requested = requestId !== null
  const meta = [title.year, title.media_type === 'tv' ? 'TV' : 'Movie'].filter(Boolean).join(' · ')

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title.title} description={title.title}>
      <div className="flex flex-col gap-6">
        <div className="flex gap-5">
          <div className="aspect-[2/3] w-28 shrink-0 overflow-hidden rounded-lg bg-poster ring-1 ring-white/10">
            {title.poster_url ? (
              <img src={title.poster_url} alt="" className="size-full object-cover" />
            ) : null}
          </div>
          <div className="min-w-0">
            <div className="font-mono text-xs text-faint">{meta}</div>
            {title.overview ? (
              <p className="mt-2 line-clamp-6 text-sm leading-relaxed text-muted">
                {title.overview}
              </p>
            ) : null}
            <div className="mt-4 flex flex-wrap gap-2">
              {requested ? (
                <span className="inline-flex items-center gap-1.5 rounded-lg bg-available/15 px-3 text-sm font-semibold text-available ring-1 ring-available/30">
                  ✓ Requested
                </span>
              ) : (
                <Button onClick={() => void onRequest()} loading={createRequest.isPending}>
                  Request
                </Button>
              )}
              <Button
                variant="secondary"
                onClick={() => void runPreview(requestId)}
                loading={searchPreview.isPending}
              >
                {requested ? 'Re-search' : 'Preview releases'}
              </Button>
            </div>
          </div>
        </div>

        {searchPreview.isPending && !preview ? (
          <CenteredSpinner label="Running the decision engine…" />
        ) : preview ? (
          <ReleaseList
            preview={preview}
            onGrab={(rel) => void onGrab(rel)}
            grabbingGuid={grabbingGuid}
            canGrab={requested}
          />
        ) : null}
      </div>
    </Dialog>
  )
}
