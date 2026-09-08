import { describe, expect, it, vi } from 'vitest'
import { COMMAND_SESSION_FOLDER, fileSessionInCommandFolder, type FolderClient } from './sessionFolder'

/** A client whose listing is fixed and whose creates hand back sequential ids. */
function fakeClient(list: unknown, opts: { createFails?: boolean; assignFails?: boolean } = {}) {
  let n = 0
  const created: Array<{ name: string; parentId?: string }> = []
  const assigned: Array<{ slotKey: string; folderId: string }> = []
  const client: FolderClient = {
    list: vi.fn(async () => list),
    create: vi.fn(async (name: string, parentId?: string) => {
      if (opts.createFails) throw new Error('refused')
      created.push({ name, parentId })
      return { id: 'new-' + ++n, name, parent_id: parentId ?? '' }
    }),
    assign: vi.fn(async (slotKey: string, folderId: string) => {
      if (opts.assignFails) throw new Error('refused')
      assigned.push({ slotKey, folderId })
      return { ok: true }
    }),
  }
  return { client, created, assigned }
}

describe('fileSessionInCommandFolder', () => {
  it('names the parent folder for the launcher that opened the session', () => {
    // Pinned because the name is DURABLE: it is matched by name on every later run, so
    // changing it orphans every session already filed under the old one.
    expect(COMMAND_SESSION_FOLDER).toBe('Command Bar Sessions')
  })

  it('creates both folders on first use and files the slot into the leaf', async () => {
    const { client, created, assigned } = fakeClient([])
    const leaf = await fileSessionInCommandFolder(client, 'slot-1', 'Approve and merge all PRs')
    expect(created).toEqual([
      { name: COMMAND_SESSION_FOLDER, parentId: undefined },
      { name: 'Approve and merge all PRs', parentId: 'new-1' },
    ])
    expect(assigned).toEqual([{ slotKey: 'slot-1', folderId: 'new-2' }])
    expect(leaf).toBe('new-2')
  })

  it('reuses both folders on a later run', async () => {
    const { client, created, assigned } = fakeClient([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: 'Approve and merge all PRs', parent_id: 'p' },
    ])
    const leaf = await fileSessionInCommandFolder(client, 'slot-2', 'Approve and merge all PRs')
    expect(created).toEqual([])
    expect(assigned).toEqual([{ slotKey: 'slot-2', folderId: 'l' }])
    expect(leaf).toBe('l')
  })

  it('gives a second command its own leaf under the shared parent', async () => {
    const { client, created, assigned } = fakeClient([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: 'Approve and merge all PRs', parent_id: 'p' },
    ])
    await fileSessionInCommandFolder(client, 'slot-3', 'Review all PRs')
    expect(created).toEqual([{ name: 'Review all PRs', parentId: 'p' }])
    expect(assigned).toEqual([{ slotKey: 'slot-3', folderId: 'new-1' }])
  })

  it('does not mistake a same-named folder elsewhere in the tree for ours', async () => {
    const { client, created } = fakeClient([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'other', name: 'Review all PRs', parent_id: 'unrelated' },
    ])
    await fileSessionInCommandFolder(client, 'slot-4', 'Review all PRs')
    expect(created).toEqual([{ name: 'Review all PRs', parentId: 'p' }])
  })

  it('treats a missing parent_id as the top level', async () => {
    const { client, created, assigned } = fakeClient([{ id: 'p', name: COMMAND_SESSION_FOLDER }])
    await fileSessionInCommandFolder(client, 'slot-5', 'Review all PRs')
    expect(created).toEqual([{ name: 'Review all PRs', parentId: 'p' }])
    expect(assigned).toEqual([{ slotKey: 'slot-5', folderId: 'new-1' }])
  })

  it('returns null and never throws when the folder API refuses', async () => {
    const { client, assigned } = fakeClient([], { createFails: true })
    await expect(fileSessionInCommandFolder(client, 'slot-6', 'Review all PRs')).resolves.toBeNull()
    expect(assigned).toEqual([])
  })

  it('returns null and never throws when the assign refuses', async () => {
    const { client } = fakeClient(
      [
        { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
        { id: 'l', name: 'Review all PRs', parent_id: 'p' },
      ],
      { assignFails: true },
    )
    await expect(fileSessionInCommandFolder(client, 'slot-7', 'Review all PRs')).resolves.toBeNull()
  })

  it('tolerates an error envelope where the folder list was expected', async () => {
    const { client, created } = fakeClient({ error: 'nope' })
    await fileSessionInCommandFolder(client, 'slot-8', 'Review all PRs')
    expect(created).toEqual([
      { name: COMMAND_SESSION_FOLDER, parentId: undefined },
      { name: 'Review all PRs', parentId: 'new-1' },
    ])
  })

  it('does nothing without a slot key or a command title', async () => {
    const { client } = fakeClient([])
    expect(await fileSessionInCommandFolder(client, '', 'Review all PRs')).toBeNull()
    expect(await fileSessionInCommandFolder(client, 'slot-9', '   ')).toBeNull()
    expect(client.list).not.toHaveBeenCalled()
  })
})
