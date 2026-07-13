import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { ToastProvider } from './components/ui/toast'
import { queryClient } from './lib/queryClient'
import { purgeLegacyApiKey } from './lib/legacyCleanup'
import { router } from './router'
import './fonts'
import './styles/index.css'

// Migrate the pre-session recovery-key remnant left in localStorage by the old
// break-glass flow onto the current cookie session, then scrub it (CodeQL
// #263). Awaited before mount so the app's first auth check already sees the
// freshly-minted cookie instead of racing it.
await purgeLegacyApiKey()

const rootEl = document.getElementById('root')
if (!rootEl) throw new Error('#root not found')

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
)
