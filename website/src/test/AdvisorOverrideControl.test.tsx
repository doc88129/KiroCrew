import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import AdvisorOverrideControl from '../components/AdvisorOverrideControl'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'

const advisorOverride = vi.spyOn(api, 'chatSlotAdvisorOverride')

function storeFor(value: 'inherit' | 'on' | 'off' = 'inherit') {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        slots: [{ key: 's1', messages: 0, running: false, advisor_override: value }],
        unreadSlots: [], refreshTrigger: 0, subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function wrap(ui: React.ReactElement, value: 'inherit' | 'on' | 'off' = 'inherit') {
  const store = storeFor(value)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return Object.assign(
    render(<Provider store={store}><QueryClientProvider client={client}>{ui}</QueryClientProvider></Provider>),
    { store },
  )
}

async function choose(label: string) {
  fireEvent.click(screen.getByRole('combobox', { name: 'Advisor mode' }))
  fireEvent.click(await screen.findByRole('option', { name: label }))
}

describe('AdvisorOverrideControl', () => {
  beforeEach(() => {
    advisorOverride.mockReset().mockResolvedValue({ ok: true, advisor_override: 'on' })
  })

  it('offers inherit, on, and off and writes the persisted value into the slot row', async () => {
    const { store } = wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)

    fireEvent.click(screen.getByRole('combobox', { name: 'Advisor mode' }))
    expect(screen.getByRole('option', { name: 'Inherit' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Enabled' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Disabled' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: 'Enabled' }))

    await waitFor(() => expect(advisorOverride).toHaveBeenCalledWith('s1', 'on'))
    await waitFor(() => expect(store.getState().dashboard.slots[0].advisor_override).toBe('on'))
  })

  it('rolls back and renders ErrorNotice when the write fails', async () => {
    advisorOverride.mockRejectedValueOnce(new Error('rejected'))
    wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)

    await choose('Enabled')

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not update Advisor.')
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Advisor mode' })).toHaveTextContent('Inherit'))
  })

  it('lives in the same per-session model panel as reasoning effort', () => {
    wrap(
      <ModelEffortDropdown
        anchorRect={{ right: 400, top: 300 } as DOMRect}
        dropdownRef={{ current: null }}
        inputRef={{ current: null }}
        models={[{ name: 'auto' }]}
        activeModel="auto"
        onSelectModel={vi.fn()}
        filter=""
        setFilter={vi.fn()}
        onClose={vi.fn()}
        hasEffort
        slot="s1"
        currentEffort="high"
        currentAdvisorOverride="off"
        onListKeyDown={vi.fn()}
      />,
      'off',
    )

    expect(screen.getByRole('button', { name: /^Reasoning/ })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Advisor mode' })).toHaveTextContent('Disabled')
    expect(screen.getByText('Reviews this session with a second model and flags issues while you work.')).toBeInTheDocument()
  })
})
