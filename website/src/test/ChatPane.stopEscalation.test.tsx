import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, fireEvent, act } from '@testing-library/react'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { FORCE_KILL_ARMING_MS } from '../utils/stopDebounce'

/* The Stop button in a pane that is NOT the active slot — the Crew Members DM
 * thread is the everyday case — must behave like the main chat's (#9547):
 *
 *  - a pending cooperative cancel is VISIBLE (the pulsing button + the
 *    "click again to force stop" hint the composer renders from `stopState`),
 *    instead of the plain armed Stop that reads as "nothing happened";
 *  - the second press while the slot is `soft_pending` escalates through the
 *    shared `handleStopPress` decision (force=true), and a double-tap inside
 *    the arming window is ignored;
 *  - a slots snapshot that says the slot is not running settles the pane's own
 *    run state, so a `_done` that never reached this tab cannot leave a Stop
 *    button behind for a turn the backend finished long ago;
 *  - a `/stop` the backend answers with `not running` settles it the same way.
 */

const apiMock = vi.hoisted(() => ({
  stopChatSlot: vi.fn(),
  stopChatSlotForce: vi.fn(),
}))

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    stopChatSlot: apiMock.stopChatSlot,
    stopChatSlotForce: apiMock.stopChatSlotForce,
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'member-default'
const OTHER = 'chat-1-main'

type Msg = { role: string; content: string; ts: string }
const USER_MSG: Msg = { role: 'user', content: 'hi', ts: '2026-09-01T00:00:00Z' }

function slotRow(over: Partial<ChatSlot> = {}): ChatSlot {
  return { key: SLOT, messages: 1, running: true, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...over } as unknown as ChatSlot
}

/** The pane's slot is NOT the active one, so its run state lives in
 *  `slotRun` — the Members DM path. */
function makeStore(runState: string, slot: Partial<ChatSlot> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [slotRow(slot)],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: OTHER,
        slotMessages: { [SLOT]: [USER_MSG] },
        slotRun: { [SLOT]: { state: runState } },
      } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
}

function mount(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChatPane slotKey={SLOT} agentLocked frameless />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  apiMock.stopChatSlot.mockReset().mockResolvedValue({ ok: true })
  apiMock.stopChatSlotForce.mockReset().mockResolvedValue({ ok: true })
})

describe('ChatPane Stop: parity with the main chat', () => {
  it('shows the pending-cancel state and the force hint while the slot is soft_pending', () => {
    const { queryByTestId } = mount(makeStore('tool_running', { stop_state: 'soft_pending', stopping: true }))
    expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
    expect(queryByTestId('stop-force-hint')).not.toBeNull()
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })

  it('first press is a soft stop; a press while soft_pending escalates to force after the arming window', () => {
    vi.useFakeTimers()
    try {
      const store = makeStore('tool_running')
      const { getByTestId, queryByTestId } = mount(store)
      fireEvent.click(getByTestId('stop-button-armed'))
      expect(apiMock.stopChatSlot).toHaveBeenCalledWith(SLOT)
      expect(apiMock.stopChatSlotForce).not.toHaveBeenCalled()

      // The backend confirms the cancel is pending.
      act(() => { store.dispatch(sseSlots([slotRow({ stop_state: 'soft_pending', stopping: true })])) })
      expect(queryByTestId('stop-button-pulsing')).not.toBeNull()

      // Inside the arming window a second press is an accidental double-tap.
      fireEvent.click(getByTestId('stop-button-pulsing'))
      expect(apiMock.stopChatSlotForce).not.toHaveBeenCalled()

      act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 5) })
      fireEvent.click(getByTestId('stop-button-pulsing'))
      expect(apiMock.stopChatSlotForce).toHaveBeenCalledWith(SLOT)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a slots snapshot saying not running settles a pane whose _done never arrived', () => {
    const store = makeStore('streaming')
    const { queryByTestId } = mount(store)
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    act(() => { store.dispatch(sseSlots([slotRow({ running: false })])) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })

  it('a snapshot that already says not running when the pane mounts does not settle a live pane', () => {
    // Opened mid-turn: live frames mark the pane busy, the snapshot predates
    // the turn. No transition was observed, so nothing is settled.
    const store = makeStore('streaming', { running: false })
    const { queryByTestId } = mount(store)
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('streaming')
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    // Once the server has been seen running and then not, the transition settles it.
    act(() => { store.dispatch(sseSlots([slotRow({ running: true })])) })
    act(() => { store.dispatch(sseSlots([slotRow({ running: false })])) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
  })

  it('a slots snapshot saying running does not promote an idle pane', () => {
    const store = makeStore('idle', { running: false })
    const { queryByTestId } = mount(store)
    act(() => { store.dispatch(sseSlots([slotRow({ running: true })])) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
    // The composer's running signal for a background pane comes from the live
    // frames; the snapshot alone does not arm a Stop button here.
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })

  it('a Stop request that fails on the wire is shown, not swallowed', async () => {
    apiMock.stopChatSlot.mockRejectedValueOnce(new Error('offline'))
    const store = makeStore('streaming')
    const { getByTestId, queryByTestId } = mount(store)
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    const notice = queryByTestId('chat-pane-stop-error')
    expect(notice).not.toBeNull()
    expect(notice?.textContent).toContain('offline')
    // The turn is still running, so the Stop button stays for a retry.
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    // A retry that succeeds retires the notice.
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: true })
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    expect(queryByTestId('chat-pane-stop-error')).toBeNull()
  })

  it('a Stop the backend answers with not running settles the pane', async () => {
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: true, info: 'not running', already_stopping: false })
    const store = makeStore('streaming')
    const { getByTestId, queryByTestId } = mount(store)
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })
})
