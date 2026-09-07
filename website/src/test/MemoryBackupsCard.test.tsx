import { fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { expect, it } from 'vitest'
import { server } from '../../integration/mocks/server'
import MemoryBackupsCard from '../pages/overview/MemoryBackupsCard'
import { renderWithProviders } from './helpers'

it.each([false, true])('stages, remounts and cancels recovery with privateMemory=%s', async privateMemory => {
  const store = privateMemory ? 'member-reviewer' : 'default'
  const name = privateMemory ? 'memory.snapshot.zip' : 'memory.snapshot.db'
  let pending = false
  const writes: unknown[] = []
  server.use(
    http.get('*/api/memory/backups', ({ request }) => {
      expect(new URL(request.url).searchParams.get('store')).toBe(store)
      return HttpResponse.json({
        backups: [{ name, size_bytes: 1000, taken_at: '2026-09-07T12:00:00Z' }],
        pending, restart_required: pending,
      })
    }),
    http.post('*/api/memory/restore', async ({ request }) => {
      writes.push(await request.json())
      pending = true
      return HttpResponse.json({ ok: true, pending: true, restart_required: true })
    }),
    http.post('*/api/memory/restore/cancel', async ({ request }) => {
      expect(await request.json()).toEqual({ store })
      pending = false
      return HttpResponse.json({ ok: true, cancelled: true, pending: false })
    }),
  )
  const view = renderWithProviders(<MemoryBackupsCard store={store} privateMemory={privateMemory} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Restore', exact: true }))
  expect(screen.getByText(/Stage this memory backup for the next gateway restart/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm restore' }))
  expect(await screen.findByText(/Restore is ready. Restart the gateway/)).toBeVisible()
  expect(writes).toEqual([{ name, store }])
  expect(screen.getByRole('button', { name: 'Restore', exact: true })).toBeDisabled()

  view.unmount()
  const remounted = renderWithProviders(<MemoryBackupsCard store={store} privateMemory={privateMemory} />)
  expect(await screen.findByText(/Restore is ready. Restart the gateway/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Restore', exact: true })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel staged restore' }))
  expect(await screen.findByText(/Staged restore cancelled/)).toBeVisible()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Restore', exact: true })).toBeEnabled())
  expect(screen.queryByRole('button', { name: 'Cancel staged restore' })).toBeNull()
  remounted.unmount()
})
