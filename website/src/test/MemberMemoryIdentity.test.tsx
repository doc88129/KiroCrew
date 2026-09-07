import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { MemoryStoreField } from '../pages/KiroCrewAgentsPage'
import JobForm from '../components/JobForm'
import { wakesCrew } from '../components/crew/wakesCrew'
import type { CronJob } from '../types'

const calls = vi.hoisted(() => ({ update: vi.fn() }))
vi.mock('../api/client', () => ({ api: {
  models: vi.fn().mockResolvedValue([]),
  updateCron: calls.update,
} }))

describe('private member memory controls', () => {
  it('creates private memory automatically without a store picker', () => {
    renderWithProviders(<MemoryStoreField />)
    expect(screen.getByText(/own empty private memory/i)).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('offers explicit initialization for a legacy member', () => {
    const initialize = vi.fn()
    renderWithProviders(<MemoryStoreField member="reviewer" value="default" onInitialize={initialize} />)
    fireEvent.click(screen.getByRole('button', { name: 'Initialize private memory' }))
    expect(initialize).toHaveBeenCalledOnce()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('displays an immutable private identity and protects unsaved edits before navigation', () => {
    const manage = vi.fn()
    renderWithProviders(<MemoryStoreField member="reviewer" value="member-reviewer-123" privateReady onManage={manage} manageDisabled />)
    expect(screen.getByText('member-reviewer-123')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Manage memory' })).toBeDisabled()
    expect(manage).not.toHaveBeenCalled()
  })
})

describe('scheduled member identity', () => {
  it('never attributes a global template job to a member with the same name', () => {
    const legacy = { agent: 'reviewer' } as CronJob
    expect(wakesCrew(legacy, 'reviewer', true)).toBe(false)
    expect(wakesCrew(legacy, 'default', false)).toBe(true)
    const member = { agent: 'shared-template', member_id: 'reviewer' } as CronJob
    expect(wakesCrew(member, 'reviewer', false)).toBe(true)
    expect(wakesCrew(member, 'shared-template', false)).toBe(false)
    expect(wakesCrew(member, 'default', true)).toBe(false)
  })

  it('keeps the member immutable while editing a scheduled task', async () => {
    calls.update.mockResolvedValue({})
    const job = { id: 'job-one', name: 'Review', message: 'Review changes', agent: 'shared-template', member_id: 'reviewer', enabled: true, schedule: 'every 1h' } as CronJob
    renderWithProviders(<JobForm job={job} agents={[]} defaultAgent="default" onSaved={() => {}} />)
    expect(screen.getByTestId('jobform-locked-agent')).toHaveTextContent('reviewer')
    expect(screen.queryByLabelText('Switch agent')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))
    await waitFor(() => expect(calls.update).toHaveBeenCalledWith('job-one', expect.objectContaining({ member_id: 'reviewer', agent: 'shared-template' })))
  })
})
