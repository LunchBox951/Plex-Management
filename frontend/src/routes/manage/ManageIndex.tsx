import { Navigate } from 'react-router'
import { useRequests } from '../../api/hooks'
import { CenteredSpinner } from '../../components/ui/feedback'
import { needsAttention } from '../../lib/status'
import { useIsPhone } from '../../lib/useMediaQuery'

/**
 * Where the Manage button lands (agreed default): Overview on a phone; on
 * desktop, Needs attention when something is waiting, otherwise Requests.
 */
export function ManageIndex() {
  const isPhone = useIsPhone()
  const requests = useRequests({ poll: true })
  if (isPhone) return <Navigate to="/manage/overview" replace />
  if (requests.isLoading) return <CenteredSpinner label="Opening Manage…" />
  const waiting = (requests.data?.requests ?? []).some((request) => needsAttention(request.status))
  return <Navigate to={waiting ? '/manage/attention' : '/manage/requests'} replace />
}
