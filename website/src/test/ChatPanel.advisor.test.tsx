import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({
    agent: { model: 'auto', reasoning_effort: '' },
    advisor: {
      enabled: false,
      model: '',
      non_blocker_budget: 4,
      cooldown_secs: 120,
      include_reasoning: false,
    },
  })),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({
      restore_sessions: false,
      restore_window_minutes: 30,
      merge_queued_messages: false,
      widget_density: 'more',
    }),
    kirocrewConfig: kirocrewConfigMock,
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider>)
}

beforeEach(() => {
  patchConfigMock.mockReset().mockResolvedValue({})
  kirocrewConfigMock.mockReset().mockResolvedValue({
    agent: { model: 'auto', reasoning_effort: '' },
    advisor: {
      enabled: false,
      model: '',
      non_blocker_budget: 4,
      cooldown_secs: 120,
      include_reasoning: false,
    },
  })
})

describe('ChatPanel — Advisor settings', () => {
  it('renders every Advisor control with the configured values and bounds', async () => {
    renderPanel()

    expect(await screen.findByRole('heading', { name: 'Advisor' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Enable Advisor' })).not.toBeChecked()
    expect(screen.getByRole('switch', { name: 'Include reasoning' })).not.toBeChecked()

    const model = screen.getByLabelText('Reviewer model') as HTMLInputElement
    expect(model.value).toBe('')
    expect(model.placeholder).toBe('Empty uses the default model')

    const budget = screen.getByLabelText('Non-blocker budget') as HTMLInputElement
    expect(budget.value).toBe('4')
    expect(budget).toHaveAttribute('min', '0')
    expect(budget).toHaveAttribute('max', '50')

    const cooldown = screen.getByLabelText('Interruption cooldown (seconds)') as HTMLInputElement
    expect(cooldown.value).toBe('120')
    expect(cooldown).toHaveAttribute('min', '0')
    expect(cooldown).toHaveAttribute('max', '3600')
  })

  it('PATCHes advisor.enabled and shows the optimistic value immediately', async () => {
    let resolve!: (value: object) => void
    patchConfigMock.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    renderPanel()

    const toggle = await screen.findByRole('switch', { name: 'Enable Advisor' })
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(toggle)

    await waitFor(() => expect(toggle).toBeChecked())
    expect(patchConfigMock).toHaveBeenCalledWith('advisor.enabled', true)
    resolve({})
  })

  it('rolls back a rejected Advisor toggle and reports the save failure', async () => {
    patchConfigMock.mockRejectedValueOnce(new Error('rejected'))
    renderPanel()

    const toggle = await screen.findByRole('switch', { name: 'Enable Advisor' })
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(toggle)

    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('advisor.enabled', true))
    await waitFor(() => expect(toggle).not.toBeChecked())
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to save Advisor setting')
  })

  it('PATCHes valid numeric edits and rejects values outside the published bounds', async () => {
    renderPanel()
    const budget = await screen.findByLabelText('Non-blocker budget') as HTMLInputElement
    const cooldown = screen.getByLabelText('Interruption cooldown (seconds)') as HTMLInputElement

    fireEvent.change(budget, { target: { value: '7' } })
    fireEvent.blur(budget)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('advisor.non_blocker_budget', 7))

    fireEvent.change(cooldown, { target: { value: '45.5' } })
    fireEvent.blur(cooldown)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('advisor.cooldown_secs', 45.5))

    patchConfigMock.mockClear()
    fireEvent.change(budget, { target: { value: '51' } })
    fireEvent.blur(budget)
    fireEvent.change(cooldown, { target: { value: '-1' } })
    fireEvent.blur(cooldown)

    expect(patchConfigMock).not.toHaveBeenCalled()
    expect(budget.value).toBe('4')
    expect(cooldown.value).toBe('120')
  })
})
