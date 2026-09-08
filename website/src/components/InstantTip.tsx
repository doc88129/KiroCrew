import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/**
 * The one instant-tooltip spelling. `PastePreviewTooltip` records why this repo
 * shares tooltip renderers ("so the two previews cannot drift"); this module is
 * the same rule applied to the hover bubble that `ResizeBadge` and the
 * follow-up chips both need. The chrome, the positioning and the show/hide
 * gesture live here once; a call site owns only its content and a width/wrap
 * variant class.
 *
 * Semantics, chosen against the native `title` this replaces:
 * - Pointer shows after a short intent delay (default 100ms) — long enough
 *   that a pointer merely crossing the element on its way somewhere else
 *   paints nothing, short enough to still read as instant. The ~1s OS delay
 *   was the defect; zero was the flicker.
 * - Keyboard focus shows synchronously. A tab stop is deliberate in a way a
 *   pointer transit is not, and a keyboard user has no second cursor to wave.
 * - Escape hides while open, without requiring blur.
 * - Any scroll hides while open: the position is captured at show time, so
 *   after a scroll the bubble would sit detached from its anchor. Capture
 *   phase, because the strips that scroll (`overflow-x-auto`) do not bubble
 *   their scroll events to window.
 */
export interface TipPos { top: number; left: number }

export function useInstantTip(openDelayMs = 100) {
  const [tip, setTip] = useState<TipPos | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const anchorRef = useRef<HTMLElement | null>(null)
  // Links the anchor to the bubble (`aria-describedby` -> `role="tooltip"`),
  // restoring what the native `title` gave screen readers for free. Applied
  // unconditionally: a described-by pointing at a not-yet-rendered id is
  // simply ignored, and a conditional one would re-announce on every show.
  const tipId = useId()

  const cancelPending = () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
  }
  const showFor = (el: HTMLElement) => {
    const r = el.getBoundingClientRect()
    setTip({ top: r.top - 8, left: r.left })
  }
  const hide = () => { cancelPending(); anchorRef.current = null; setTip(null) }

  useEffect(() => () => cancelPending(), [])

  // Escape and scroll dismiss only while open, so the listeners exist only
  // while open. The rect goes stale the moment anything scrolls; hiding is
  // strictly better than a bubble stranded at old coordinates.
  useEffect(() => {
    if (!tip) return
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') hide() }
    const onScroll = () => hide()
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('scroll', onScroll, { capture: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tip !== null])

  const tipHandlers = {
    'aria-describedby': tipId,
    onMouseEnter: (e: React.MouseEvent) => {
      cancelPending()
      const el = e.currentTarget as HTMLElement
      anchorRef.current = el
      // Rect is read when the timer fires, not at enter: the anchor can move
      // in the intent window (an entrance animation settling, a layout shift).
      timerRef.current = setTimeout(() => {
        timerRef.current = null
        if (anchorRef.current === el && el.isConnected) showFor(el)
      }, openDelayMs)
    },
    onMouseLeave: () => hide(),
    onFocus: (e: React.FocusEvent) => {
      cancelPending()
      const el = e.currentTarget as HTMLElement
      anchorRef.current = el
      showFor(el)
    },
    onBlur: () => hide(),
  }

  return { tip, tipHandlers, tipId }
}

/** The bubble. Shared chrome here; the caller passes only content and a
 *  variant class for width/wrap (`whitespace-nowrap` for a short two-liner,
 *  `max-w-[26rem] whitespace-pre-wrap break-words` for prose). Pass the
 *  hook's `tipId` so the anchor's `aria-describedby` resolves. */
export function InstantTip({ tip, tipId, className = '', children }: {
  tip: TipPos | null
  tipId?: string
  className?: string
  children: React.ReactNode
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [clampedLeft, setClampedLeft] = useState<number | null>(null)
  // The anchor-left position is measured before the bubble exists, so its
  // width is unknowable at show time. Clamp to the viewport after first
  // paint: a right-edge anchor otherwise pushes a `position: fixed` bubble
  // past window.innerWidth, clipping exactly the long labels the tooltip
  // exists to recover. Left-edge overflow cannot occur (left starts at the
  // anchor's own on-screen left), so one side suffices.
  useLayoutEffect(() => {
    setClampedLeft(null)
    if (!tip) return
    const el = ref.current
    if (!el) return
    const overflow = tip.left + el.offsetWidth - (window.innerWidth - 8)
    if (overflow > 0) setClampedLeft(Math.max(8, tip.left - overflow))
  }, [tip])
  if (!tip) return null
  return createPortal(
    <div
      ref={ref}
      id={tipId}
      role="tooltip"
      className={`fixed z-[9999] -translate-y-full rounded-lg border border-border-strong bg-bg-elevated px-2.5 py-1.5 text-[11px] leading-snug shadow-lg pointer-events-none ${className}`}
      style={{ top: tip.top, left: clampedLeft ?? tip.left }}
    >
      {children}
    </div>,
    document.body,
  )
}
