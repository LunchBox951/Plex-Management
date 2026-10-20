import { Navigate, createBrowserRouter } from 'react-router'
import { AdminGate } from './components/AdminGate'
import { Layout } from './components/Layout'
import { SetupGate } from './components/SetupGate'
import { Blocklist } from './routes/Blocklist'
import { Discover } from './routes/Discover'
import { Logs } from './routes/Logs'
import { ManageIndex } from './routes/manage/ManageIndex'
import { ManageOverview } from './routes/manage/ManageOverview'
import { ManageRequests } from './routes/manage/ManageRequests'
import { NeedsAttention } from './routes/manage/NeedsAttention'
import { NotFound } from './routes/NotFound'
import { Notifications } from './routes/Notifications'
import { PlexPopupLoading } from './routes/PlexPopupLoading'
import { QualityProfile } from './routes/QualityProfile'
import { Queue } from './routes/Queue'
import { Requests } from './routes/Requests'
import { Settings } from './routes/Settings'
import { SetupWizard } from './routes/SetupWizard'
import { Status } from './routes/Status'

/** Pre-overhaul admin paths, kept as redirects so bookmarks and old links still work. */
const LEGACY_ADMIN_PATHS = [
  ['queue', '/manage/downloads'],
  ['status', '/manage/health'],
  ['logs', '/manage/logs'],
  ['settings', '/manage/settings'],
  ['blocklist', '/manage/blocklist'],
  ['quality', '/manage/quality'],
] as const

export const router = createBrowserRouter([
  // The wizard lives outside the gate so it is reachable pre-init (the backend
  // allowlists it too; see SetupGuardMiddleware).
  { path: '/setup', element: <SetupWizard /> },
  { path: '/login/plex/loading', element: <PlexPopupLoading /> },
  {
    element: <SetupGate />,
    children: [
      {
        element: <Layout />,
        children: [
          { index: true, element: <Discover showActivity /> },
          { path: 'movies', element: <Discover mediaType="movie" /> },
          { path: 'tv', element: <Discover mediaType="tv" /> },
          { path: 'requests', element: <Requests /> },
          { path: 'notifications', element: <Notifications /> },
          {
            element: <AdminGate />,
            children: [
              {
                path: 'manage',
                children: [
                  { index: true, element: <ManageIndex /> },
                  { path: 'overview', element: <ManageOverview /> },
                  { path: 'attention', element: <NeedsAttention /> },
                  { path: 'requests', element: <ManageRequests /> },
                  { path: 'downloads', element: <Queue /> },
                  { path: 'blocklist', element: <Blocklist /> },
                  { path: 'quality', element: <QualityProfile /> },
                  { path: 'settings', element: <Settings /> },
                  { path: 'logs', element: <Logs /> },
                  { path: 'health', element: <Status /> },
                ],
              },
              ...LEGACY_ADMIN_PATHS.map(([path, to]) => ({
                path,
                element: <Navigate to={to} replace />,
              })),
            ],
          },
        ],
      },
    ],
  },
  { path: '*', element: <NotFound /> },
])
