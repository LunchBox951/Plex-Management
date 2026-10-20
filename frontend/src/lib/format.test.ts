import { describe, expect, it } from 'vitest'
import { formatBytes, formatRelative, formatTimestamp } from './format'

describe('formatBytes', () => {
  it('renders bytes below 1024 with no decimal', () => {
    expect(formatBytes(512)).toBe('512 B')
  })

  it('renders KB/MB/GB with one decimal place', () => {
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 ** 3)).toBe('5.0 GB')
  })

  it('never throws on a non-positive or non-finite input — honesty over a crash', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(-5)).toBe('0 B')
    expect(formatBytes(Number.NaN)).toBe('0 B')
  })
})

describe('formatTimestamp', () => {
  it('renders "never" for a null/undefined instant', () => {
    expect(formatTimestamp(null)).toBe('never')
    expect(formatTimestamp(undefined)).toBe('never')
  })

  it('renders a locale datetime string for an ISO instant', () => {
    const rendered = formatTimestamp('2026-01-01T00:00:00Z')
    expect(rendered).not.toBe('never')
    expect(rendered.length).toBeGreaterThan(0)
  })
})

describe('formatRelative', () => {
  const now = Date.parse('2026-10-20T12:00:00Z')

  it('reads recent times as short relative ages', () => {
    expect(formatRelative('2026-10-20T11:59:40Z', now)).toBe('now')
    expect(formatRelative('2026-10-20T11:55:00Z', now)).toBe('5m')
    expect(formatRelative('2026-10-20T09:00:00Z', now)).toBe('3h')
    expect(formatRelative('2026-10-18T12:00:00Z', now)).toBe('2d')
  })

  it('falls back to a short date after a week, and to nothing for garbage', () => {
    expect(formatRelative('2026-10-01T12:00:00Z', now)).not.toMatch(/^\d+[mhd]$/)
    expect(formatRelative('not a date', now)).toBe('')
  })
})
