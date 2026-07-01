import { createBrowserRouter } from 'react-router-dom'
import { Layout } from './components/Layout'
import { SetupGate } from './components/SetupGate'
import { Blocklist } from './routes/Blocklist'
import { Discover } from './routes/Discover'
import { Logs } from './routes/Logs'
import { NotFound } from './routes/NotFound'
import { QualityProfile } from './routes/QualityProfile'
import { Queue } from './routes/Queue'
import { Requests } from './routes/Requests'
import { Settings } from './routes/Settings'
import { SetupWizard } from './routes/SetupWizard'
import { Status } from './routes/Status'

export const router = createBrowserRouter([
  // The wizard lives outside the gate so it is reachable pre-init (the backend
  // allowlists it too; see SetupGuardMiddleware).
  { path: '/setup', element: <SetupWizard /> },
  {
    element: <SetupGate />,
    children: [
      {
        element: <Layout />,
        children: [
          { index: true, element: <Discover /> },
          { path: 'requests', element: <Requests /> },
          { path: 'queue', element: <Queue /> },
          { path: 'status', element: <Status /> },
          { path: 'logs', element: <Logs /> },
          { path: 'settings', element: <Settings /> },
          { path: 'blocklist', element: <Blocklist /> },
          { path: 'quality', element: <QualityProfile /> },
        ],
      },
    ],
  },
  { path: '*', element: <NotFound /> },
])
