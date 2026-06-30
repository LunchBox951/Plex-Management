import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Button } from './Button'

describe('Button', () => {
  // Regression: `disabled ?? loading` left a spinning button clickable when a
  // caller passed an explicit `disabled={false}` (double-submit).
  it('stays disabled while loading even with an explicit disabled={false}', () => {
    render(
      <Button loading disabled={false}>
        Submit
      </Button>,
    )
    expect(screen.getByRole('button')).toBeDisabled()
  })

  it('is enabled when neither disabled nor loading', () => {
    render(<Button>Go</Button>)
    expect(screen.getByRole('button')).toBeEnabled()
  })
})
