import { Link } from 'react-router-dom'
import { Button } from '../components/ui/Button'

export function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-bg px-5 text-center">
      <div className="font-display text-5xl font-extrabold text-gold">404</div>
      <p className="text-muted">That page doesn't exist.</p>
      <Link to="/">
        <Button variant="secondary">Back to Discover</Button>
      </Link>
    </div>
  )
}
