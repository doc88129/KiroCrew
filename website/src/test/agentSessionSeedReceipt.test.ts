/**
 * A seed prompt that provably never ran must not leave a recorded, empty
 * session -- and a seed that MAY be running must never be torn down.
 *
 * Two app seeders guard this -- Issue Radar and Auto Improvement. Both used to
 * read the raw `api.sendChat` response through `readSendReceipt` themselves;
 * both now call the chat-core transport `sendTurn` and branch on its receipt
 * status. This file pins that mapping for BOTH seeders, so the contract cannot
 * drift between them again:
 *
 *   - `refused`         -> slot deleted, nothing recorded, the server's own
 *                          reason surfaces (a refusal inside a 200 is the case a
 *                          status-only check used to miss).
 *   - `unknown`         -> NOT deleted, recorded. Accepted, receipt unreadable:
 *                          deleting would cancel real work over a mangled reply.
 *   - `response-late`   -> NOT deleted, recorded. Deadline hit before a receipt:
 *                          delivery is indeterminate and a running seed must
 *                          not be destroyed for being slow.
 *   - `transport-error` -> NOT deleted, NOT recorded, error surfaced. A fetch
 *                          also rejects when the connection resets AFTER the
 *                          server took the POST, so delivery is indeterminate:
 *                          a possibly-running seed is never cancelled, and the
 *                          failure is reported as the old bare fetch did.
 *   - `dispatched` / `queued` -> recorded normally.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { SendReceipt } from '../chat-core/transport/sendTurn'
import { i18nT } from '../i18n/t'

const { dispatch, apiMock, sendTurn, saveInvestigation, getInvestigation } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    renameSlot: vi.fn(),
  },
  sendTurn: vi.fn(),
  saveInvestigation: vi.fn(),
  getInvestigation: vi.fn(),
}))

vi.mock('../store', () => ({ useAppDispatch: () => dispatch }))
vi.mock('../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  deleteSlot: (arg: unknown) => ({ type: 'deleteSlot', arg }),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))
vi.mock('../chat-core/transport/sendTurn', () => ({ sendTurn }))
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: { saveInvestigation, getInvestigation } }))

import { useAgentSession as useIssueRadarSession } from '../apps/issue-radar/lib/agentSession'
import { useAgentSession as useAutoImproveSession } from '../apps/auto-improvement/lib/agentSession'

const receipt = (status: SendReceipt['status'], reason?: string): SendReceipt =>
  ({ status, body: {}, ...(reason ? { reason } : {}) })

/** Did the SUT ask for the freshly created slot to be deleted? */
const deletedSlot = () =>
  dispatch.mock.calls.some((c) => (c[0] as { type: string }).type === 'deleteSlot')

beforeEach(() => {
  vi.resetAllMocks()
  dispatch.mockImplementation((action: { type: string }) => ({
    unwrap: () =>
      action.type === 'createSlot'
        ? Promise.resolve({ key: 'slot-1' })
        : Promise.resolve(undefined),
  }))
  apiMock.chatFolders.mockResolvedValue([
    { id: 'repo-1', name: 'Issue Radar - demo-repo' },
    { id: 'repo-2', name: 'Auto-Improve - acme/demo-repo' },
  ])
})

describe('Issue Radar seed — the sendTurn receipt decides', () => {
  /** Open a session and hand back the result plus the LIVE hook handle:
   *  `openSession` swallows the throw, returns null, and reports through `error`
   *  — which lands in a later render, so it is read via `waitFor`, never off the
   *  snapshot taken before the await. */
  async function open() {
    const { result } = renderHook(() => useIssueRadarSession())
    const opened = await result.current.openSession({
      repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
      number: 4237,
      title: '#4237 · seed refused',
      prompt: 'seed',
      existing: null,
    })
    return { opened, result }
  }

  beforeEach(() => {
    getInvestigation.mockResolvedValue({ investigation: null })
    saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'slot-1' } })
  })

  it('sends the seed through the transport, addressed to the new slot', async () => {
    sendTurn.mockResolvedValue(receipt('dispatched'))
    await open()
    expect(sendTurn).toHaveBeenCalledWith({ message: 'seed', slot: 'slot-1' })
  })

  it('tears down the slot on `refused` and keeps the server\'s reason', async () => {
    // The refusal-inside-a-200 case a status-only check missed entirely.
    sendTurn.mockResolvedValue(receipt('refused', 'slot is stopping'))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error?.message).toBe(
      `${i18nT('pages.chatPage.could_not_start_a_new_session')} (slot is stopping)`,
    ))
    expect(deletedSlot()).toBe(true)
    // ...and nothing is recorded, so no row points at a session that never ran.
    expect(saveInvestigation).not.toHaveBeenCalled()
  })

  it('still tears down on a reasonless refusal (a plain non-2xx), in human words', async () => {
    sendTurn.mockResolvedValue(receipt('refused'))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    // No raw status enum and no English wrapper around a localized fragment:
    // the whole message is the core's own "could not start" copy.
    await waitFor(() => expect(result.current.error?.message).toBe(String(i18nT('pages.chatPage.could_not_start_a_new_session'))))
    expect(deletedSlot()).toBe(true)
    expect(saveInvestigation).not.toHaveBeenCalled()
  })

  it('reports `transport-error` but keeps the slot and records nothing — indeterminate', async () => {
    // A reset after dispatch rejects the fetch exactly like a request that
    // never left, so the slot (and any turn running in it) survives; the user
    // still sees that the launch did not confirm, as before.
    sendTurn.mockResolvedValue(receipt('transport-error'))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error?.message).toBe(String(i18nT('pages.chatPage.could_not_start_a_new_session'))))
    expect(deletedSlot()).toBe(false)
    expect(saveInvestigation).not.toHaveBeenCalled()
  })

  it.each([
    ['unknown', 'the seed may be running'],
    ['response-late', 'slow is not refused'],
  ] as const)('does NOT tear down on `%s` — %s', async (status) => {
    sendTurn.mockResolvedValue(receipt(status))

    const { result } = await open()
    expect(result.current.error).toBeNull()
    expect(deletedSlot()).toBe(false)
    expect(saveInvestigation).toHaveBeenCalled()
  })

  it.each(['dispatched', 'queued'] as const)('records normally on `%s`', async (status) => {
    sendTurn.mockResolvedValue(receipt(status))

    const { result } = await open()
    expect(result.current.error).toBeNull()
    expect(deletedSlot()).toBe(false)
    expect(saveInvestigation).toHaveBeenCalled()
  })
})

describe('Auto Improvement seed — the same receipt contract', () => {
  const saved = vi.fn()

  /** The record store is this app's own backend, reached with bare `fetch`:
   *  GET answers "no record yet", PUT is captured so the test can tell whether a
   *  session was recorded. */
  beforeEach(() => {
    saved.mockReset()
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        saved(init.body)
        return { ok: true, json: async () => ({ session: { slot_key: 'slot-1', status: 'open' } }) }
      }
      return { ok: true, json: async () => ({ session: null }) }
    }))
  })

  async function open() {
    const { result } = renderHook(() => useAutoImproveSession())
    const opened = await result.current.openSession({
      kind: 'pr',
      id: 42,
      repo: 'acme/demo-repo',
      title: 'PR #42 · seed',
      prompt: 'seed',
    })
    return { opened, result }
  }

  it('sends the seed through the transport, addressed to the new slot', async () => {
    sendTurn.mockResolvedValue(receipt('dispatched'))
    await open()
    expect(sendTurn).toHaveBeenCalledWith({ message: 'seed', slot: 'slot-1' })
  })

  it.each([
    ['with the server\'s reason', 'slot agent mismatch', /slot agent mismatch/],
    ['with the core\'s own copy when the body carried none', undefined, String(i18nT('pages.chatPage.could_not_start_a_new_session'))],
  ] as const)('tears down and records nothing on `refused` %s', async (_label, reason, expected) => {
    sendTurn.mockResolvedValue(receipt('refused', reason))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error?.message).toMatch(expected))
    expect(deletedSlot()).toBe(true)
    expect(saved).not.toHaveBeenCalled()
  })

  it('keeps the slot but records nothing and reports on `transport-error`', async () => {
    sendTurn.mockResolvedValue(receipt('transport-error'))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error).toBeTruthy())
    expect(deletedSlot()).toBe(false)
    expect(saved).not.toHaveBeenCalled()
  })

  it.each(['unknown', 'response-late', 'dispatched', 'queued'] as const)(
    'keeps the slot and records the session on `%s`',
    async (status) => {
      sendTurn.mockResolvedValue(receipt(status))

      const { opened, result } = await open()
      expect(opened).not.toBeNull()
      expect(result.current.error).toBeNull()
      expect(deletedSlot()).toBe(false)
      expect(saved).toHaveBeenCalled()
    },
  )
})
