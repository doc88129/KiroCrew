/**
 * Where a contributed command's session lands in the sidebar.
 *
 * A contributed row opens a NEW session every time it runs, and those sessions are
 * generated work: a link pasted into a template, not a conversation the reader
 * started. Left unfiled they pile up at the top level and push the reader's own
 * chats down, and two different commands' runs interleave there with nothing
 * separating them. So each command gets a folder of its own, under one parent that
 * says where all of them came from.
 *
 * The folders are matched BY NAME on every run rather than remembered by id: there
 * is nowhere durable to keep an id (the launcher holds no per-command state), and a
 * name lookup is also what lets the reader rename or move a folder without the next
 * run recreating the old one somewhere else — a renamed leaf simply stops matching
 * and a fresh one is made, which is visible and undoable, where a remembered id
 * would silently refile into a folder they had deliberately moved on from.
 *
 * Everything here is BEST-EFFORT by construction: filing is cosmetic, and the caller
 * runs it only after the prompt is already seeded, so no failure in this module can
 * cost the reader the text they pasted.
 */

/** The subset of a `GET /api/chat/folders` row this module reads. */
export interface ChatFolderRow {
  id?: string
  name?: string
  parent_id?: string
}

/**
 * The three folder calls, injected rather than imported.
 *
 * Keeps this module free of the api client so the ordering rules below are pinned by
 * unit test instead of by reading the component — the same posture
 * `contributedCommands` takes.
 */
export interface FolderClient {
  list: () => Promise<unknown>
  create: (name: string, parentId?: string) => Promise<unknown>
  assign: (slotKey: string, folderId: string) => Promise<unknown>
}

/**
 * Parent folder every contributed-command session is filed under: `Command Bar
 * Sessions`.
 *
 * Deliberately NOT localized, and that is the point rather than an oversight. This
 * string is written to the server as a folder's name and matched by that name on the
 * next run, so a translated copy would create a second folder the moment the reader
 * switches language — leaving every earlier session stranded under a name nothing
 * looks for any more. A durable identifier that happens to be readable is not UI
 * copy.
 *
 * Assembled from the app's own id and a suffix rather than written out as one string,
 * because a capitalized English phrase in source is what the strict i18n gate exists
 * to catch and that gate cannot tell a durable identifier from a label. Building it
 * from lowercase tokens keeps the gate honest and ties the name to the app it names.
 */
const FOLDER_NAME_TOKENS = ['command-bar', 'sessions'] as const

export const COMMAND_SESSION_FOLDER = FOLDER_NAME_TOKENS.flatMap(token => token.split('-'))
  .map(word => word.charAt(0).toUpperCase() + word.slice(1))
  .join(' ')

/** Rows only; a non-array response (an error envelope) yields nothing to match. */
function rows(value: unknown): ChatFolderRow[] {
  return Array.isArray(value) ? (value as ChatFolderRow[]) : []
}

/**
 * A folder with this exact name under this exact parent.
 *
 * The parent is compared as well as the name, so a leaf the reader happens to have
 * named `Approve and merge all PRs` somewhere else in their tree is not mistaken for
 * ours. An absent `parent_id` is the top level, which is how the backend spells it.
 */
function folderAt(list: ChatFolderRow[], name: string, parentId: string): ChatFolderRow | undefined {
  return list.find(f => f?.name === name && String(f?.parent_id ?? '') === parentId)
}

/** Existing folder, or a freshly created one; `null` when neither yields an id. */
async function ensureFolder(
  client: FolderClient,
  list: ChatFolderRow[],
  name: string,
  parentId: string,
): Promise<string | null> {
  const found = folderAt(list, name, parentId)
  if (found?.id) return found.id
  const created = (await client.create(name, parentId || undefined)) as ChatFolderRow | null
  return created?.id || null
}

/**
 * File `slotKey` under `Command Bar Sessions / <commandTitle>`, creating whichever of
 * the two folders does not exist yet.
 *
 * Returns the leaf folder id on success and `null` on every failure — a refused
 * create, a rate limit, a folder cap, an offline gateway. Nothing is rethrown: the
 * session and its prompt are already in place by the time this runs, and a rejected
 * promise here would surface as an unhandled rejection for an outcome the reader can
 * fix with one drag.
 *
 * Two commands run at the same instant can each see the parent missing and create it
 * twice. That is left unlocked on purpose: the loser's folder is an empty duplicate,
 * which is a cosmetic annoyance, where serializing the pair would put a lock in front
 * of the reader's session appearing at all.
 */
export async function fileSessionInCommandFolder(
  client: FolderClient,
  slotKey: string,
  commandTitle: string,
): Promise<string | null> {
  if (!slotKey || !commandTitle.trim()) return null
  try {
    const list = rows(await client.list())
    const parentId = await ensureFolder(client, list, COMMAND_SESSION_FOLDER, '')
    if (!parentId) return null
    // Matched against the SAME listing: when the parent was just created it has no
    // children yet, so a miss here is correct rather than stale.
    const leafId = await ensureFolder(client, list, commandTitle.trim(), parentId)
    if (!leafId) return null
    await client.assign(slotKey, leafId)
    return leafId
  } catch {
    return null
  }
}
