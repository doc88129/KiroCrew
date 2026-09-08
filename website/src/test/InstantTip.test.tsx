import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { InstantTip, useInstantTip } from '../components/InstantTip'

/** Minimal consumer: one anchor button + the shared bubble. */
function Harness({ openDelayMs }: { openDelayMs?: number }) {
  const { tip, tipHandlers, tipId } = useInstantTip(openDelayMs)
  return (
    <>
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </>
  )
}

// The gesture semantics live in the shared module, so they are pinned here
// once rather than per consumer. FollowUpBar / ChatInput tests assert only
// their own tooltip CONTENT, via keyboard focus (the synchronous path).
describe('InstantTip', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows synchronously on keyboard focus — a tab stop is deliberate', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('shows after the hover-intent delay on pointer enter, not immediately', () => {
    render(<Harness />)
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('paints nothing for a pointer passing through inside the intent window', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.mouseEnter(anchor)
    fireEvent.mouseLeave(anchor)
    act(() => { vi.advanceTimersByTime(200) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('hides on mouse leave', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.mouseLeave(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('Escape dismisses while open, without requiring blur', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('any scroll dismisses — the captured rect is stale after a scroll', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.scroll(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('blur hides the focus-shown bubble', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.blur(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('links the anchor to the bubble via aria-describedby', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    const described = anchor.getAttribute('aria-describedby')
    expect(described).toBeTruthy()
    expect(screen.getByRole('tooltip').id).toBe(described)
  })

  it('clamps the bubble inside the right viewport edge', () => {
    // jsdom has no layout: give every element a measured width for this test.
    const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, value: 300 })
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true })
    try {
      render(<Harness />)
      const anchor = screen.getByRole('button', { name: 'anchor' })
      // Anchor near the right edge: 1000 + 300 would overflow 1024.
      anchor.getBoundingClientRect = () => ({ top: 200, left: 1000, right: 1010, bottom: 210, width: 10, height: 10, x: 1000, y: 200, toJSON: () => ({}) }) as DOMRect
      fireEvent.focus(anchor)
      const left = parseFloat(screen.getByRole('tooltip').style.left)
      expect(left + 300).toBeLessThanOrEqual(1024 - 8)
      expect(left).toBeGreaterThanOrEqual(8)
    } finally {
      if (saved) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', saved)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
    }
  })
})
