/**
 * Initial labels remain readable; selection changes still animate.
 *
 * This is the negative half of `SegmentedControl.reducedMotion.test.tsx`, kept in
 * its own file on purpose: framer-motion reads `prefers-reduced-motion` once into
 * module state that `vi.resetModules()` does not clear, so two opposite
 * preferences cannot be exercised in one file — whichever ran first would decide
 * the answer for both.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

const SEGMENTS = [
  { key: 'grid' as const, label: 'Gallery' },
  { key: 'table' as const, label: 'Table' },
]

describe('SegmentedControl — motion kept for everyone else', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('shows initial labels immediately and animates later compact selection changes', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }))
    const { default: SegmentedControl } = await import('../components/SegmentedControl')
    const { rerender } = render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={() => {}} compact />)
    expect(screen.getByText('Gallery').style.width).not.toBe('0px')
    rerender(<SegmentedControl segments={SEGMENTS} value="table" onChange={() => {}} compact />)
    expect(screen.getByText('Table').style.width).toBe('0px')
  })
})
