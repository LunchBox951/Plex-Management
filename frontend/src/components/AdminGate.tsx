import { Navigate, Outlet } from 'react-router-dom'
import { useAuthMe } from '../api/hooks'
import { CenteredSpinner } from './ui/feedback'

export function AdminGate() {
  const auth = useAuthMe()
  if (auth.isLoading) return <CenteredSpinner label="Checking access..." />
  const isAdmin = auth.data?.is_admin ?? auth.data?.user?.is_admin ?? false
  if (!isAdmin) return <Navigate to="/" replace />
  return <Outlet />
}
