import { randomUUID } from 'node:crypto'
import { test, expect, type APIRequestContext, type Page } from '@playwright/test'

// These writes must never target an operator gateway. The offline E2E harness
// owns the seeded home and deletes it, including retained member archives.
test.beforeEach(() => {
  expect(process.env.KIROCREW_E2E_EPHEMERAL, 'Use the isolated gateway E2E harness').toBe('1')
})

async function member(request: APIRequestContext, role: string) {
  const name = `memory-e2e-${role}-${randomUUID().slice(0, 8)}`
  const response = await request.post('/api/agents', { data: { name, kiro_agent: 'kirocrew' } })
  expect(response.ok(), await response.text()).toBeTruthy()
  const created = await response.json()
  expect(created.memory_store).toMatch(/^member-/)
  return { name, store: created.memory_store as string }
}

async function rows(request: APIRequestContext, store: string, q = '') {
  const response = await request.get('/api/memory/semantic', { params: { store, limit: 100, q } })
  expect(response.ok(), await response.text()).toBeTruthy()
  return (await response.json()).entries as { key: string; value_json: string; derived_from?: string }[]
}

async function writeFact(request: APIRequestContext, store: string, key: string, value: string) {
  const response = await request.put('/api/memory/semantic', {
    headers: { 'X-Session-Key': 'dashboard:ui' },
    params: { store }, data: { key, value, source: 'user_explicit' },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
}

async function openMemory(page: Page, owner: { name: string; store: string }) {
  await page.goto(`/settings/overview?view=memory&store=${encodeURIComponent(owner.store)}`)
  await expect(page.getByText(`Memory for ${owner.name}`, { exact: true })).toBeVisible()
}

test('member memory copy, correction and forgetting persist without changing V1 or another member', async ({ page, request }, testInfo) => {
  test.setTimeout(90000)
  const target = await member(request, 'reviewer')
  const other = await member(request, 'writer')
  expect(target.store).not.toBe(other.store)
  expect(await rows(request, target.store)).toEqual([])
  expect(await rows(request, other.store)).toEqual([])

  const key = `user.e2e_${randomUUID().replaceAll('-', '')}`
  const original = `Lighthouse ${key.slice(-8)} uses the blue review checklist.`
  const corrected = `Lighthouse ${key.slice(-8)} uses the green review checklist.`
  await writeFact(request, 'default', key, original)
  const globalBefore = (await rows(request, 'default', key)).find(row => row.key === key)
  expect(globalBefore).toBeDefined()

  await openMemory(page, target)
  await page.getByRole('button', { name: 'Choose starting knowledge' }).click()
  const copy = page.getByRole('dialog', { name: 'Copy knowledge' })
  await copy.getByRole('textbox').fill(key)
  await copy.getByRole('checkbox', { name: original, exact: true }).check()
  await copy.getByRole('button', { name: 'Copy selected (1)' }).click()
  await expect(copy.getByRole('status')).toContainText('1')
  await copy.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(page.getByText(original, { exact: true })).toBeVisible()
  const seeded = (await rows(request, target.store)).find(row => row.key === key)
  expect(JSON.parse(seeded!.derived_from!)).toMatchObject({ store: 'default' })

  await page.getByRole('button', { name: 'Correct', exact: true }).click()
  const correction = page.getByRole('dialog', { name: 'Correct memory' })
  await correction.getByRole('textbox', { name: 'Correct', exact: true }).fill(corrected)
  await correction.getByRole('button', { name: 'Preview changes', exact: true }).click()
  await correction.getByRole('button', { name: 'Apply changes (1)', exact: true }).click()
  await expect(correction).toBeHidden()
  await page.reload()
  await expect(page.getByText(corrected, { exact: true })).toBeVisible()
  await expect(page.getByText(`Memory for ${target.name}`, { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Choose starting knowledge', exact: true })).toBeEnabled()
  expect((await rows(request, target.store)).find(row => row.key === key)?.derived_from).toBe(seeded?.derived_from)
  expect((await rows(request, 'default', key)).find(row => row.key === key)).toEqual(globalBefore)
  expect(await rows(request, other.store)).toEqual([])

  await page.screenshot({ path: testInfo.outputPath('member-memory-desktop.png'), fullPage: true, animations: 'disabled' })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByText(corrected, { exact: true })).toBeVisible()
  for (const label of ['All', 'Facts', 'Rules', 'Experiences']) {
    await expect(page.getByText(label, { exact: true })).toBeVisible()
    await expect(page.getByText(label, { exact: true })).toBeInViewport({ ratio: 1 })
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({ path: testInfo.outputPath('member-memory-mobile.png'), fullPage: true, animations: 'disabled' })
  await page.setViewportSize({ width: 1280, height: 720 })

  await page.getByRole('button', { name: 'View details', exact: true }).click()
  await page.getByRole('dialog', { name: 'Memory details' }).getByRole('button', { name: 'Forget', exact: true }).click()
  await page.getByRole('dialog', { name: 'Forget' }).getByRole('button', { name: 'Preview changes', exact: true }).click()
  await page.getByRole('dialog', { name: 'Forget' }).getByRole('button', { name: 'Apply changes (1)', exact: true }).click()
  await expect.poll(async () => (await rows(request, target.store)).length).toBe(0)
  await page.reload()
  await expect(page.getByText(corrected, { exact: true })).toHaveCount(0)
  expect((await rows(request, 'default', key)).find(row => row.key === key)).toEqual(globalBefore)
})

test('private backup staging survives navigation and can be cancelled without replacing active memory', async ({ page, request }) => {
  test.setTimeout(90000)
  const owner = await member(request, 'recovery')
  const key = 'user.e2e_restore'
  await writeFact(request, owner.store, key, 'The backup contains the earlier decision.')
  await openMemory(page, owner)
  await page.getByRole('tab', { name: 'Recovery', exact: true }).click()
  await page.getByRole('button', { name: 'Back up now', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Restore', exact: true })).toHaveCount(1)
  await writeFact(request, owner.store, key, 'The active member keeps the newer decision.')
  const active = await rows(request, owner.store)
  await page.getByRole('button', { name: 'Restore', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm restore', exact: true }).click()
  await expect(page.getByText(/Restore is ready\. Restart the gateway/)).toBeVisible()
  await page.reload()
  await page.getByRole('tab', { name: 'Recovery', exact: true }).click()
  await expect(page.getByText(/Restore is ready\. Restart the gateway/)).toBeVisible()
  expect(await rows(request, owner.store)).toEqual(active)
  await page.getByRole('button', { name: 'Cancel staged restore', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Restore', exact: true })).toBeEnabled()
  expect(await rows(request, owner.store)).toEqual(active)
})

test('an empty private memory opens its exact member conversation and reuses the persisted thread after reload', async ({ page, request }) => {
  test.setTimeout(90000)
  const owner = await member(request, 'conversation')
  const readOwner = async () => {
    const response = await request.get('/api/members')
    expect(response.ok(), await response.text()).toBeTruthy()
    const roster = (await response.json()).members as {
      name: string; slug: string; slot_key: string; memory_store: string; memory_version: number; memory_owner: string
    }[]
    const row = roster.find(candidate => candidate.name === owner.name)
    expect(row, 'The exact created member must be present in the real roster').toBeDefined()
    return row!
  }
  const initial = await readOwner()
  expect(initial).toMatchObject({
    slot_key: '', memory_store: owner.store, memory_version: 2, memory_owner: owner.name,
  })
  expect(await rows(request, owner.store)).toEqual([])

  const threadPath = `/api/members/${encodeURIComponent(initial.slug)}/thread`
  const waitForThread = () => page.waitForResponse(response =>
    response.request().method() === 'POST' && new URL(response.url()).pathname === threadPath,
  )
  await openMemory(page, owner)
  const opened = waitForThread()
  await page.getByRole('button', { name: 'Open member conversation', exact: true }).click()
  const response = await opened
  expect(response.ok(), await response.text()).toBeTruthy()
  const binding = await response.json() as { member: string; slug: string; slot_key: string }
  expect(binding).toMatchObject({ member: owner.name, slug: initial.slug })
  expect(binding.slot_key).not.toBe('')
  await expect(page).toHaveURL(url => url.pathname === '/members' && url.searchParams.get('member') === owner.name)
  const memberHeader = page.getByTestId('member-thread-header')
  await expect(memberHeader.getByText(owner.name, { exact: true })).toBeVisible()
  await expect(memberHeader.getByRole('button', { name: 'Edit member', exact: true })).toBeAttached()
  await expect(page.getByPlaceholder(/message/i)).toBeVisible()

  // The roster reads dm.json from disk, so this checks the saved binding rather
  // than inferring persistence from the currently mounted chat pane.
  await expect.poll(readOwner).toMatchObject({
    slot_key: binding.slot_key, memory_store: owner.store, memory_owner: owner.name,
  })
  const reopened = waitForThread()
  await page.reload()
  const reloadedResponse = await reopened
  expect(reloadedResponse.ok(), await reloadedResponse.text()).toBeTruthy()
  expect(await reloadedResponse.json()).toEqual(binding)
  await expect(memberHeader.getByText(owner.name, { exact: true })).toBeVisible()
  await expect(page.getByPlaceholder(/message/i)).toBeVisible()
  expect(await rows(request, owner.store)).toEqual([])
})


for (const lineage of ['V1', 'V2'] as const) {
  test(`${lineage} previews and atomically edits 65 email memories across pages, preserving a concurrent correction`, async ({ page, request }, testInfo) => {
    test.setTimeout(120000)
    const owner = await member(request, `bulk-${lineage.toLowerCase()}`)
    const store = lineage === 'V1' ? 'default' : owner.store
    const untouchedStore = lineage === 'V1' ? owner.store : 'default'
    const token = randomUUID().replaceAll('-', '')
    const oldEmail = `old-${token}@example.com`
    const newEmail = `new-${token}@example.com`
    const key = (index: number) => `user.bulk_${token}_${index}`
    // Bounded concurrent fixture writes use only this harness's ephemeral home.
    for (let first = 0; first < 65; first += 5) {
      await Promise.all(Array.from({ length: Math.min(5, 65 - first) }, (_, step) => {
        const index = first + step
        return writeFact(request, store, key(index), `Contact ${index}: ${oldEmail}`)
      }))
    }
    const records = async (scope: string, q: string) => {
      const response = await request.get('/api/memory/records', { params: { store: scope, q, kind: 'all', topic: 'email', limit: 100 } })
      expect(response.ok(), await response.text()).toBeTruthy()
      return await response.json() as { total: number; entries: { id: string; text: string; metadata: { email_addresses: string[] } }[] }
    }
    expect((await records(store, oldEmail)).total).toBe(65)
    if (lineage === 'V2') await openMemory(page, owner)
    else await page.goto('/settings/overview?view=memory')
    const editor = page.getByTestId('memory-records-editor')
    await editor.getByRole('textbox', { name: 'Search memory', exact: true }).fill(oldEmail)
    await editor.getByRole('button', { name: 'Email', exact: true }).click()
    await expect(editor.getByText('Matching memories: 65', { exact: true })).toBeVisible()
    await editor.getByRole('checkbox', { name: 'Select this page', exact: true }).check()
    await editor.getByRole('button', { name: 'Select all 65 matching memories', exact: true }).click()
    await editor.getByRole('button', { name: 'Next page', exact: true }).click()
    await expect(editor.getByText('51–65 of 65', { exact: true })).toBeVisible()
    await expect(editor.getByText('Selected: 65', { exact: true })).toBeVisible()
    await editor.getByRole('button', { name: 'Find and replace', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit selected memories', exact: true })
    await dialog.getByLabel('Find text', { exact: true }).fill(oldEmail)
    await dialog.getByLabel('Replace with', { exact: true }).fill(newEmail)
    await dialog.getByRole('button', { name: 'Preview changes', exact: true }).click()
    await expect(dialog.getByText('Will change: 65', { exact: true })).toBeVisible()
    await expect(dialog.getByText(/Showing 25 of 65 changes/)).toBeVisible()
    expect((await records(store, oldEmail)).total).toBe(65)
    expect((await records(store, newEmail)).total).toBe(0)

    // A real concurrent write invalidates the whole batch; no row may be partly changed.
    const concurrent = `Concurrent owner correction: ${oldEmail}`
    await writeFact(request, store, key(0), concurrent)
    const rejected = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/memory/bulk/apply')
    await dialog.getByRole('button', { name: 'Apply changes (65)', exact: true }).click()
    expect((await rejected).status()).toBe(409)
    await expect(dialog.getByRole('button', { name: 'Refresh selected records', exact: true })).toBeVisible()
    expect((await records(store, oldEmail)).total).toBe(65)
    expect((await records(store, newEmail)).total).toBe(0)
    await dialog.getByRole('button', { name: 'Refresh selected records', exact: true }).click()
    await expect(dialog.getByLabel('Replace with', { exact: true })).toHaveValue(newEmail)
    await dialog.getByRole('button', { name: 'Preview changes', exact: true }).click()
    await expect(dialog.getByText('Will change: 65', { exact: true })).toBeVisible()
    await page.setViewportSize({ width: 390, height: 844 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
    await page.screenshot({ path: testInfo.outputPath(`memory-${lineage.toLowerCase()}-bulk-preview-mobile.png`), fullPage: false, animations: 'disabled' })
    await dialog.getByRole('button', { name: 'Apply changes (65)', exact: true }).click()
    await expect(dialog).toBeHidden()
    await expect(editor.getByText('Updated records: 65', { exact: true })).toBeVisible()
    const updated = await records(store, newEmail)
    expect(updated.total).toBe(65)
    expect(updated.entries.find(row => row.id === key(0))?.text).toContain('Concurrent owner correction:')
    expect(updated.entries.every(row => row.metadata.email_addresses.includes(newEmail))).toBeTruthy()
    expect((await records(store, oldEmail)).total).toBe(0)
    expect((await records(untouchedStore, newEmail)).total).toBe(0)
    await page.reload()
    await page.getByTestId('memory-records-editor').getByRole('textbox', { name: 'Search memory', exact: true }).fill(newEmail)
    await expect(page.getByText('Matching memories: 65', { exact: true })).toBeVisible()
  })
}


for (const lineage of ['V1', 'V2'] as const) {
  const scenario = lineage === 'V1'
    ? 'refuses an automated overwrite and persists an explicit owner correction'
    : 'reviews proposals by keeping the current value and accepting a later proposal'
  test(`${lineage} ${scenario}`, async ({ page, request }) => {
    test.setTimeout(90000)
    const owner = await member(request, `review-${lineage.toLowerCase()}`)
    const store = lineage === 'V1' ? 'default' : owner.store
    const token = randomUUID().replaceAll('-', '')
    const key = `user.proposal_${token}`
    const currentValue = `Current contact: owner-${token}@example.com`
    const firstProposal = `Unconfirmed contact: wrong-${token}@example.com`
    const acceptedValue = `Confirmed contact: team-${token}@example.com`
    type RecordState = { id: string; value_json: string; source: string; revision: string; metadata: { revision: number; pending_conflicts: number } }
    const readRecord = async () => {
      const response = await request.get('/api/memory/records', { params: { store, q: key, kind: 'fact', limit: 50 } })
      expect(response.ok(), await response.text()).toBeTruthy()
      const result = await response.json() as { total: number; entries: RecordState[] }
      expect(result.total).toBe(1)
      expect(result.entries[0].id).toBe(key)
      return result.entries[0]
    }
    const readHistory = async () => {
      const response = await request.get('/api/memory/records/history', { params: { store, kind: 'fact', id: key, limit: 25 } })
      expect(response.ok(), await response.text()).toBeTruthy()
      return await response.json() as { current_revision: number; entries: { id: number; base_revision: number; operation: string; status: string; after_json: string }[] }
    }
    const propose = async (value: string) => {
      const response = await request.put('/api/memory/semantic', {
        headers: { 'X-Session-Key': 'dashboard:ui' }, params: { store },
        data: { key, value, source: 'consolidation', confidence: 0.9 },
      })
      expect(response.status(), await response.text()).toBe(409)
      const error = (await response.json()).error
      if (lineage === 'V1') {
        expect(error).toBe('Existing entry set by user cannot be overwritten by automated source')
        expect((await readRecord()).metadata.pending_conflicts).toBe(0)
      } else {
        expect(error).toContain('saved for review')
        await expect.poll(async () => (await readRecord()).metadata.pending_conflicts).toBe(1)
      }
    }
    const filterRecord = async () => {
      const editor = page.getByTestId('memory-records-editor')
      await editor.getByRole('textbox', { name: 'Search memory', exact: true }).fill(key)
      await expect(editor.getByText('Matching memories: 1', { exact: true })).toBeVisible()
      return editor
    }

    await writeFact(request, store, key, currentValue)
    const initial = await readRecord()
    await propose(firstProposal)
    expect((await readRecord()).value_json).toBe(initial.value_json)
    if (lineage === 'V2') await openMemory(page, owner)
    else await page.goto('/settings/overview?view=memory')
    let editor = await filterRecord()
    if (lineage === 'V1') {
      expect(await readRecord()).toEqual(initial)
      await expect(editor.getByRole('button', { name: /Review proposals/ })).toHaveCount(0)
      await editor.getByRole('button', { name: 'Correct', exact: true }).click()
      const correction = page.getByRole('dialog', { name: 'Correct memory', exact: true })
      await correction.getByRole('textbox', { name: 'Correct', exact: true }).fill(acceptedValue)
      await correction.getByRole('button', { name: 'Preview changes', exact: true }).click()
      await expect(correction.getByText('Will change: 1', { exact: true })).toBeVisible()
      expect(await readRecord()).toEqual(initial)
      await correction.getByRole('button', { name: 'Apply changes (1)', exact: true }).click()
      await expect(correction).toBeHidden()
      const corrected = await readRecord()
      expect(JSON.parse(corrected.value_json)).toBe(acceptedValue)
      expect(corrected.source).toBe('user_explicit')
      expect(corrected.metadata).toMatchObject({ revision: initial.metadata.revision + 1, pending_conflicts: 0 })
      const history = await readHistory()
      expect(history.current_revision).toBe(corrected.metadata.revision)
      expect(history.entries[0]).toMatchObject({ operation: 'correct', status: 'accepted' })
      await page.reload()
      editor = await filterRecord()
      await expect(editor.getByText(acceptedValue, { exact: true })).toBeVisible()
      await expect(editor.getByRole('button', { name: /Review proposals/ })).toHaveCount(0)
      expect(await readRecord()).toEqual(corrected)
      return
    }
    await editor.getByRole('button', { name: 'Review proposals (1)', exact: true }).click()
    let detail = page.getByRole('dialog', { name: 'Memory details', exact: true })
    await expect(detail.getByText(firstProposal, { exact: true })).toBeVisible()
    const resolutionPreview = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/memory/bulk/preview')
    await detail.getByRole('button', { name: 'Keep current value', exact: true }).click()
    const previewResponse = await resolutionPreview
    expect(previewResponse.ok(), await previewResponse.text()).toBeTruthy()
    const resolution = await previewResponse.json()
    expect(resolution.changed_count).toBe(1)
    expect(resolution.entries[0].operation).toBe('resolve')
    expect(resolution.entries[0].before).toEqual(resolution.entries[0].after)
    let correction = page.getByRole('dialog', { name: 'Correct memory', exact: true })
    await expect(correction.getByText('The current value stays unchanged. Applying records your decision and closes its pending proposals.', { exact: true })).toBeVisible()
    // Preview alone must leave the proposal pending and the record version intact.
    expect((await readRecord()).metadata).toMatchObject({ revision: initial.metadata.revision, pending_conflicts: 1 })
    await correction.getByRole('button', { name: 'Apply changes (1)', exact: true }).click()
    await expect(correction).toBeHidden()
    await expect.poll(async () => (await readRecord()).metadata.pending_conflicts).toBe(0)
    const kept = await readRecord()
    expect(kept.value_json).toBe(initial.value_json)
    expect(kept.source).toBe(initial.source)
    expect(kept.metadata.revision).toBe(initial.metadata.revision + 1)
    const keptHistory = await readHistory()
    expect(keptHistory.current_revision).toBe(kept.metadata.revision)
    expect(keptHistory.entries[0]).toMatchObject({ operation: 'resolve', status: 'accepted' })
    expect(keptHistory.entries.some(entry => entry.status === 'conflict' && entry.after_json.includes(firstProposal))).toBeTruthy()
    await page.reload()
    editor = await filterRecord()
    await expect(editor.getByText(currentValue, { exact: true })).toBeVisible()
    await expect(editor.getByRole('button', { name: /Review proposals/ })).toHaveCount(0)

    await propose(acceptedValue)
    await page.reload()
    editor = await filterRecord()
    await editor.getByRole('button', { name: 'Review proposals (1)', exact: true }).click()
    detail = page.getByRole('dialog', { name: 'Memory details', exact: true })
    await expect(detail.getByText(acceptedValue, { exact: true })).toBeVisible()
    await expect(detail.getByRole('button', { name: 'Use proposed value', exact: true })).toHaveCount(1)
    await detail.getByRole('button', { name: 'Use proposed value', exact: true }).click()
    correction = page.getByRole('dialog', { name: 'Correct memory', exact: true })
    await expect(correction.getByRole('textbox', { name: 'Correct', exact: true })).toHaveValue(acceptedValue)
    await correction.getByRole('button', { name: 'Preview changes', exact: true }).click()
    await expect(correction.getByText('Will change: 1', { exact: true })).toBeVisible()
    expect((await readRecord()).value_json).toBe(initial.value_json)
    await correction.getByRole('button', { name: 'Apply changes (1)', exact: true }).click()
    await expect(correction).toBeHidden()
    await expect.poll(async () => (await readRecord()).metadata.pending_conflicts).toBe(0)
    const accepted = await readRecord()
    expect(JSON.parse(accepted.value_json)).toBe(acceptedValue)
    expect(accepted.metadata.revision).toBe(kept.metadata.revision + 1)
    const acceptedHistory = await readHistory()
    expect(acceptedHistory.current_revision).toBe(accepted.metadata.revision)
    expect(acceptedHistory.entries[0].status).toBe('accepted')
    expect(acceptedHistory.entries[0].after_json).toContain(acceptedValue)
    await page.reload()
    editor = await filterRecord()
    await expect(editor.getByText(acceptedValue, { exact: true })).toBeVisible()
    await expect(editor.getByRole('button', { name: /Review proposals/ })).toHaveCount(0)
  })
}
