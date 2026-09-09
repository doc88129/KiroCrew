# Memory v2 — design, decisions, and handoff

The [algorithm effectiveness report](algorithm-effectiveness-report.md) collects
the measured retrieval results and their limits; a [visual edition](algorithm-effectiveness-report.html)
uses the same text and data.

The latest user decision applies **only to private V2**: Memory V2
removes age-based ranking, age-tiered history reduction and automatic episode
capacity eviction. V2 also skips model-guessed lesson deletion and background
whole-file replacement of private preference/project anchors. Explicit
forgetting, fact validity and evidence-backed correction remain. Current owner
identity, permanent rules, bound persona and admitted project guides now use
the complete essential-context path across member execution lifecycles. Report
section 10 records this contract and its evidence separately from the original
V2 model benchmark before the retention-policy change. V1 keeps its existing
algorithm and prompt behavior; this change does not migrate or retune it.

The implementation adds shared revision metadata, optional Memory MCP recall,
bounded shared embedding work and the paged/bulk editor. The original
Global-V1-unchanged requirement remains controlling for retrieval and prompt
behavior. Fresh V1 sessions retain preference/project context, decayed daily
history, semantic retrieval, query-ranked episodic fragments (top eight, at most
the smaller of 3,000 chars and the scaled episodic cap), and query-ranked scoped
lessons. Warm follow-ups use native conversation history without repeating that
injection. Only V2 uses essential anchors, query-free scoped lessons and explicit
fragment recall. Shared tools do not convert V1 or replace its session context.

V1 also keeps its automatic consolidation policy: the original semantic prompt,
confidence-based conflict resolution, confidence-1.0 source escalation, direct
stale-key deletion, same-value refresh and user-explicit lesson origin. V2 alone
uses inferred conflict proposals and verified correction evidence. Select this
policy by `algorithm_version`, because an unowned store can use the crew schema
while retaining V1 behavior. Shared owner editing and revision checks remain.
The report's section 9 separates their evidence from the original retrieval
benchmark. Current source contracts remain in the owning specs linked below.

The follow-up is implemented and validated in the separate memory worktree:
54 Linux editing/API/route-startup checks, 63 frontend editing/navigation checks,
and seven real browser flows plus authentication (8/8). Real WSL gateway
restart restored content, metadata and revisions without touching sibling/V1
stores; production stdio MCP isolation checks also passed. The browser caught
and drove a fix for desktop/mobile layout remounts discarding bulk previews.
The final 10,000-row-per-lineage measurement and accuracy limitations are in
report section 9. Changes are local; no commit, push or V1 conversion is implied.

The completion audit also fixes new classified facts being misrouted as
conflicts, requires complete latest-user correction evidence, freezes and
revalidates the transcript before consolidation writes, and bounds actual
HTTP/MCP memory output including escaped evidence and previews. Editor refresh
recounts query selections, preserves recoverable missing-record drafts, and
supports older revision pages. Current targeted Linux groups pass: schema 90,
consolidation 114, recall/stdio 215 and editing/API 85; frontend editing/navigation
passes 88 checks. These groups overlap and are not an aggregate test count.
Fresh real-browser checks pass all three changed editor flows in each lineage;
the isolated gateway and its fixture data are cleaned up. The visual report
retains both charts and has no page overflow at desktop or 390-pixel width.
Report section 9 distinguishes this completion audit from the earlier runs.

> Archived handoff for the memory v2 work. Owning specs:
> [memory-skills-hooks](../../../../system-specs/modules/memory-skills-hooks.md),
> [security](../../../../system-specs/modules/security.md),
> [learn-cron-dashboard](../../../../system-specs/modules/learn-cron-dashboard.md).
> Those specs are authoritative for current behaviour. This file records what they
> cannot: why each choice was made, which conclusions were corrected mid-flight, and
> what a following session should pick up.

## 1. What memory v2 is

Before this work, memory was **one shared pool that no crew owned and that nothing
backed up**. A crew could be given a `memory_store` name in `config.json`, but the
field was decoration: nothing read it on most paths, no surface could create a store,
and the dashboard showed the default store while reading as if it showed everything.

Memory v2 makes memory **per-crew, durable, and inspectable**, under one hard
constraint that shaped every decision:

> The operator's existing global memory must not move, and must keep behaving exactly
> as it did.

That constraint is the reason for the two-lineage design in §2 and for the
absent-parameter rule in §7. It is also why `test/test_memory_v1_golden.py` is the
acceptance proof: it is never edited, and it passing unedited is the claim.

**Scope note.** "Memory v2 is crew-members-only": silos activate only on an explicit,
non-default `memory_store` binding. An install that sets nothing keeps byte-identical
behaviour. This was a standing requirement, not an optimisation.

## 2. Two schema lineages, one engine

A memory store is a **file boundary**, not a column. There is no `workspace_id`, no
tenant column, and no cutover.

| | default store | crew silo |
|---|---|---|
| `schema_version` | `1`, `2`, `3` (v1 migrations) | `1001` (`CREW_SCHEMA_VERSION`) |
| tables | `semantic_memory`, `episodic_memories`, … | `memory_items` |
| `semantic_memory` | real table | **read-only view** over `memory_items` |
| carve facets | absent | 5 columns, outside every view |
| vector file | `<home>/memory.db` | `<home>/memory_stores/<name>/memory.db` |
| markdown root | `<home>/workspace/` | `<home>/memory_stores/<name>/` |
| FTS index | `<home>/memory_index.db` | inside the store's own directory |

Owned by `src/kiro_crew/memory_schema.py` (`LINEAGE_V1`, `LINEAGE_CREW`,
`CREW_SCHEMA_VERSION`, `MIGRATIONS_CREW`, `detect_lineage`).

### 2.1 Why `detect_lineage` reads the schema table first

`detect_lineage(db)` reads **SQLite's own `sqlite_master`** first, and consults the
file's *path* only for a file that has no product tables at all.

This is the mechanism that makes "the operator's memory is untouched" a property of
**the code path**, not of a test:

* Every memory file that exists today has v1 product tables, so it answers `v1`.
* Therefore **no code path leads from a populated file into the crew migrations.**

A path-first rule would have been the obvious implementation and is the wrong one: a
store directory that a restore or a rename dropped a v1 file into would be migrated in
place. Structure beats location because structure is what the reader actually depends
on.

`CREW_SCHEMA_VERSION = 1001` is deliberately **disjoint** from `{1,2,3}`: `init()`
applies migrations by set membership, so a crew file can never accidentally satisfy a
v1 migration's guard.

### 2.2 Why the views are read-only, and must stay that way

`semantic_memory` and `episodic_memories` survive on a silo as **views in v1's exact
column order**, so no ranker, exporter, or reader needed changing.

**Never give those views an `INSTEAD OF` trigger.** `snapshot_redact`'s
`_refuse_update_triggers_that_destroy_rows` requires `target == tbl_name`, which such a
trigger can never satisfy — so adding one makes the redaction pass **refuse the whole
product database by name**. This is the single most expensive-to-rediscover
constraint in the file.

### 2.3 Where the design doc was not followed

The proposal this work started from (an external design doc, not in-repo) was followed
on `memory_items` and rejected elsewhere, with reasons:

| Doc said | Shipped | Why |
|---|---|---|
| `UNIQUE (kind, key)` | `UNIQUE (key)` | v1 is `key TEXT PRIMARY KEY`. Per-kind uniqueness lets one key exist as both a directive and a fact, and the compatibility view then returns two rows for one key. |
| `created_at REAL` | `created_at TEXT` (ISO-8601) | Seven sites rank lexicographically on this column, one of them the episodic **cap eviction**. A `REAL` would silently reorder eviction. |
| `embedding_dim` column | dropped | Nothing read it and it drifted on six backfill paths. |
| `REFERENCES` clauses | dropped | `PRAGMA foreign_keys` is `0` and per-connection, so they document a constraint that is not enforced. |
| `WITHOUT ROWID` | plain table | `snapshot_redact`'s rowid-alias check drops the whole DB otherwise. |
| `workspaces`, `workspace_repos` | refused | Constant within a single-store file; they encode an axis this design deliberately does not have. |
| `personas` | refused | `config.json` is the source of truth, and a `read_set` column would put an **authorization value inside agent-reachable data**. |
| `migration_state` | refused | There is no cutover. The doc's own `schema_version >= 3` gate is already satisfied everywhere. |

**The design doc is stale about this fork in several verified ways. Do not build
against it as written.**

## 3. Facets: the carve axes

Five columns on `memory_items`, all `NOT NULL DEFAULT ''`:

`scope`, `surface`, `crew`, `session_key`, `derived_from`

Derived from the `MemoryFacets` dataclass — `FACET_NAMES` is
`tuple(field.name for field in fields(MemoryFacets))`, so the query allowlist cannot
drift from the dataclass. `GROUPABLE_COLUMNS` is the facets **plus `kind`**; `kind` is
groupable but is *not* a facet, because it is the row type rather than a stamped
attribution.

**Facets are absent from both compatibility views.** That is what keeps a facet
unreadable by anything that ranks — a ranker cannot accidentally score on `crew`.

Writes stamp facets through `_stamp_facets`, which is **crew-only, never raises, and
rolls back on failure**. With `isolation_level=""` a failed DML leaves the transaction
open, so the rollback is load-bearing.

`_session_facets(meta, key)` derives them: `crew` from `meta["agent"]` (**the crew
alias — never `kiro_agent`, which is a kiro-cli template id from a disjoint
namespace**), `surface` from `messaging.link.telemetry_channel_of` (bounded set),
`session_key` from the key.

An **empty facet value is meaningful**: `?crew=` selects the rows no writer
attributed, which is a different question from omitting the filter. Both the API and
the CLI transmit the mapping key-by-key rather than filtering on truthiness.

Every identifier reaching SQL is one of `memory_schema`'s own literals; a caller's
mapping is consulted for **membership only**. `_HOSTILE_NAMES` in
`test/test_memory_v2_facet_read.py` proves the refusal *and* that the table survives —
"it raised" and "it did not execute" are different claims.

On the v1 lineage, facet queries **refuse** with `FacetsUnsupported` (409
`facets_unsupported`) rather than returning empty. An empty page there reads as "this
crew has no memories" about a store holding thousands of unfaceted rows.

## 4. Episodic retirement

`src/kiro_crew/vector_memory.py` — `_retire_stale_episodic`, `_retire_one_episodic`,
`get_retired_episodic`, `restore_episodic`.

The original behaviour: a semantic write tombstoned episodic rows by similarity, with
**no cap and no visibility**. On a store rebuilt hours earlier, 21 of 101 rows were
already gone, 14 of them to this rule.

Three changes:

1. **Bounded** — `_MAX_EPISODIC_RETIRED_PER_WRITE = 3`
   (`vector_memory_constants.py`).
2. **Containment required** — a candidate must *textually restate* the superseded
   value, not merely rank near it.
3. **Recoverable** — `kirocrew memory retired --restore <id>`, and
   `GET /api/memory/retired` + `POST /api/memory/retired/restore`.

### 4.1 The cheap existence probe, and its two traps

Before embedding anything, a `text LIKE '%value%'` probe runs. It is a **provable
superset** of everything either retirement arm can act on, because a candidate must
contain the superseded value and both text-fallback patterns are substrings of it.
This took the pass from 20 embeds + 20 searches to 0 in the common case.

Two non-obvious constraints, both in the code as comments:

* **Probe the PRE-casefold value.** SQLite's `LIKE` is ASCII-case-insensitive while
  `str.casefold()` is Unicode, so a folded needle can match *more* than `LIKE` does —
  probing the folded value would skip rows the arms would then act on.
* **A value that strips to nothing retires nothing.** An empty needle originally
  skipped *both* the probe and the containment test, leaving the arms retiring on
  cosine alone. Found by a test written for the probe itself.

### 4.2 Correction to an earlier claim

An earlier report said "49.8% of episodic rows were irreversibly tombstoned". **That
was wrong.** The soft-deleted rows are physically present with full text; only
`memory_events` takes a hard `DELETE`. Retirement is invisible, not destructive, which
is why restore is cheap.

## 5. Admission threshold — measured, still untuned

`src/kiro_crew/eval/bench/admission.py`, `admission_corpus.py`,
`test/test_episodic_admission_bench.py`.

The recorded `F1 = 0.980` for the episodic admission threshold is **protocol-dependent
rather than false**:

* Under 1:1 class balance it reproduces at **0.976**.
* But under that protocol **every threshold in `[0.51, 0.58]` scores ≥ 0.98**, so that
  protocol *cannot* have selected the `0.55` the code uses.
* Under realistic class balance the pooled figure is **0.360**.

The benchmark now reports the protocol alongside the number instead of asserting a
bare figure.

**The larger defect is still open**: the long-text relaxation constant claims a `0.13`
dilution where the corpus measures **0.079**. Nobody has retuned it. See §11.

## 6. Backups

`src/kiro_crew/memory_backup.py`. `BACKUP_DIR_NAME = "backups"`, `DEFAULT_KEEP = 7`,
`MIN_BACKUP_INTERVAL_HOURS = 20`. Scheduled from `heartbeat.py`
(`_MEMORY_BACKUP_TICKS = 1440`, `_MEMORY_BACKUP_OFFSET = 30`) on
`maintenance_executor`. Config: `memory.backup_enabled` (default `True`),
`memory.backup_keep` (default `7`).

Memory is the only data here that **cannot be rebuilt from another source**, and its
durability story was a manual `kirocrew snapshot` nobody runs. That was verified the
hard way: when a live 36 MB store was truncated to 29 bytes, `snapshots/` did not
exist.

Five properties, each with a reason and a test:

1. **Consistent under a live writer** — taken through SQLite's **online backup API**,
   not a file copy. Under WAL, copying `memory.db` alone yields a file that *parses*
   while missing the committed tail sitting in its `-wal` sibling. Nothing complains.
   This is the failure mode the whole module exists to avoid.
2. **Three outcomes, not two** — `Path` on success, `None` when there was nothing to
   copy, `MemoryBackupFailed` when a copy was attempted and did not land. Folding the
   third into `None` is what originally made the caller's `failed` counter
   unreachable, so a store failing every single day logged nothing.
3. **Interval guard reads the stamped filename, not the mtime.** The heartbeat tick
   counter is per process and resets on every gateway start, so without the guard a
   gateway restarted five times a day writes five copies and the seven-day window
   collapses to ~1.4 days. A restored or touched file carries a new mtime while its
   name still tells the truth.
4. **Atomic** — written to `.partial` and renamed, so an interrupted run leaves
   nothing that looks like a backup.
5. **Non-destructive restore** — verifies `PRAGMA integrity_check` on the source
   **before** displacing anything, then moves the existing file aside as
   `memory.db.superseded.<stamp>` and removes the stale `-wal`/`-shm` (a leftover WAL
   describes a database that is no longer there, and SQLite would replay it onto the
   restored file).

### 6.1 Two things that surprise people

* **A restore takes effect on the next gateway start** for a store this gateway
  already has open. The displaced file is *renamed*, and an open SQLite connection
  follows the **inode**, not the name. There is no reopen to call: `init()` reassigns
  the connection with no idempotence guard, and it runs `PRAGMA journal_mode=WAL`,
  which raises on exactly the corrupt file the route exists to replace. The UI says
  this on the success path.
* **`backup`, `backups` and `restore` dispatch BEFORE the store is opened**, because
  opening it runs `PRAGMA journal_mode=WAL` — which raises on the corrupt file those
  verbs exist to repair. The most-needed path would otherwise be the one that cannot
  run.
* **Backup names collide across stores by construction**: `back_up_all_stores` takes
  one `stamp` for the whole sweep and every store's file stem is `memory`. Any UI
  state keyed on a bare backup name must be reset when the store changes (this caused
  a real bug; see §8).

## 7. Store resolution and the security argument

### 7.1 Two roots, one name

`memory_stores.py` is a **leaf module** (stdlib-only at import time) so `security` can
depend on it without a cycle. Conflating its two resolvers is the sharpest hazard:

* `memory_store_dir_for(store)` → the **markdown** root. `"default"` →
  `memory.workspace_dir()`, *not* the data home, or every install's `preferences.md`
  moves out from under both the consolidator and `kirocrew memory search`.
* `resolve_store_path(store)` → the **vector file**. `"default"` →
  `config_dir()/"memory.db"`, byte-identical to `VectorMemoryStore()`'s own default.
* `memory_index_path_for(store)` → a third answer. The default store's FTS index stays
  in the data-home **root**, because that is the only location the snapshot `memory`
  component, `portability`'s export zip and `scripts/sync-to-remote.sh` name.

### 7.2 Two failure postures, deliberately different

* An **undeclared** name **degrades** (`degrade_store_name`), two logged hops ending at
  the always-declared `"default"` floor. A raise would land inside
  `HistoryConsolidator._consolidate`'s `try`, be caught while `billed` is still
  `False` so no backoff is recorded, and re-arm every 60s idle tick **forever**.
* A **malformed** name **raises** `UnknownMemoryStore`, because degrading is the one
  case that silently merges two crews' memory: `../work` and `Work` must not resolve
  onto a store that exists.

Those stay consistent only because `usable_store_names()` makes a malformed name
*undeclared for resolution*, and **both** membership tests in the tree run through it.

### 7.3 Positive identity, and identity-not-containment

`named_store_of_db(path)` is the only **positive** spelling of "this file is a crew
silo". The negation a caller reaches for — `path != config_dir()/"memory.db"` — is true
of four real non-silo paths (the eval runner's file, the bench ingest path, the
onboarding importer's destination, and every `tmp_path` in the suite), and would hand
each of them silo treatment.

Its containment test is **identity**, not containment, and the difference is a real
isolation hole: with `memory_stores/acme` symlinked at `memory_stores/finance`, a
resolved-*parent* check still sees the root and would answer `"acme"` for a file that
physically belongs to `finance`. `_named_store_dir` refuses the same aliasing in the
forward direction.

`owned_store_path(store)` is the resolve-then-**confirm** pair, and it exists because
`resolve_store_path` degrades: a caller that skips the confirm silently reads,
reports, or copies the operator's own memory *under another store's name*.

### 7.4 The dashboard owner gate — read this before touching `?store=`

Every memory route takes an optional `?store=`. The rule lives in one place,
`handlers/_shared.py :: resolve_requested_memory_store`.

**The parameter's PRESENCE is the gate.**

* **Absent** → the answer that adds no reach (§7.5).
* **Present** → `require_owner_dashboard_request`.

The gate excludes an agent **positively**, and this is a cross-module property worth
stating exactly:

> `token_auth_middleware`'s `X-Internal-Secret` branch (kiro-cli, MCP, subagents) calls
> `await handler(request)` **without ever setting `request["user"]`**. The
> cookie/query-token branch sets it. `is_owner_dashboard_request` requires it
> non-empty.

So an agent fails the gate because it has **no identity to present** — not because it
was recognised and rejected. A negation (`not request.get("internal_auth")`) would fail
toward the permissive answer when a third caller class appears, which is the shape
`AGENTS.md` bans for harness identity.

`test/test_memory_store_dashboard.py` pins this: adding `request["user"]` beside
`request["internal_auth"] = True` turns it red across all 55 internal paths. **Keep
that test.** The property spans three modules and can be broken by an unrelated edit.

Two more rules in the same function:

* Gating on **presence**, not on "the name differs from my binding": `?store=default`
  names the operator's own global memory, so a mismatch-only rule would wave through
  the most sensitive value the parameter can carry whenever the caller happened to be
  unbound.
* An **undeclared name is a 404**, never a degrade. `resolve_store_path` degrades onto
  the default store, which here would render the operator's own memory under a label
  for a store that does not exist — and the request would look like it worked. A
  malformed name gets the *same* answer as an unknown one, so the refusal does not
  report whether a given name is declared to a caller that has not passed the gate.

### 7.5 The absent-parameter rule — an authorization boundary, not a default

Every store-scoped `/api/memory/*` content route now resolves an absent `store`
parameter to the global store, INCLUDING carve. The former `ABSENT_GLOBAL` /
`ABSENT_BINDING` selector has been removed so a new route cannot opt back into the
unverified session-header path.

The earlier handoff incorrectly exempted carve as pre-existing behavior. Carve was
introduced by this feature branch. A non-owner dashboard token could send another
session's `X-Session-Key` and receive that session's silo rows or facet counts without
passing the owner gate. Both requests were reproduced as HTTP 200 before the fix;
they now read global v1 and return `409 facets_unsupported`. Explicit named-store
reads still require the owner, and unavailable stores never fall back to global.

The agent-facing `/api/lessons` routes have a separate contract because agents must
be able to record corrections in their own store. `resolve_lesson_memory_store`
allows a named binding only for a middleware-verified internal request
(`internal_auth is True`) or a verified dashboard owner. Non-owner dashboard tokens
and App Kit tokens cannot read, create or delete another session's silo lessons by
supplying its session key. The raw internal-secret header is never proof. Six
non-owner/app-token cases reproduced HTTP 200 before this gate; owner and verified
internal requests retain their existing access.

`_recognize_session` establishes that a key exists, not that it belongs to the caller.
Do not use its success as authorization to follow a named memory binding.

## 8. The dashboard UI

`website/src/pages/overview/` — `MemoryStoreCard.tsx` (picker + overview + New store),
`MemoryCarveCard.tsx`, `MemoryRetiredCard.tsx`, `MemoryBackupsCard.tsx`, and
`MemoryTab.tsx`'s internal `MemoryDocCard`.

The tab previously carried a note admitting the gap: *"No route behind this page takes
a memory-store name … the page shows a subset of the memory that exists while reading
as if it were all of it."* That note is gone because the gap is closed.

### 8.1 Displayed store vs wire store — do not collapse these

`store` state is the **wire** value. `''` means *no parameter is sent*. The picker
**displays** `store || active`, where `active` comes from `GET /api/memory/stores`.

Collapsing them cost two real bugs:

1. **Silent data loss that reported success.** A seeding effect flipped `store` from
   `''` to `'default'` once the listing landed. The document cards are keyed on
   `store`, so that flip **remounted** them and discarded a typed draft — and the
   following Save wrote the **stale server copy** back under a green "Saved" badge.
2. **A regression on the default path.** Sending `?store=default` is semantically
   identical to sending nothing but takes the owner gate, so on an install with no
   configured owner the whole Memory page would refuse.

`pick(name)` therefore sends `''` when `name === active`, and the name otherwise.

### 8.2 Other UI invariants with reasons

* `MemoryDocCard`'s save clears its draft **only if the draft is still what was
  saved** (`setDraft(prev => prev === saved ? null : prev)`). A PUT is not instant and
  the textarea stays editable; an unconditional reset discards every keystroke typed
  during the request.
* Each store-scoped card carries `key={store}` so a switch **remounts** it and drops
  local state. Without it an *armed* restore survives a store switch, and because
  backup names collide across stores (§6.1) the same-named row of the new store
  renders already-confirmed — one click restores a store nobody armed.
* `facets_unsupported` renders an explanation, **never an empty list**.
* Cards that do **not** follow the picker (settings, embedding model, lessons, the
  vector browser) say so on screen.
* `GET /api/memory/carve` answers `{"store": ""}` for the global store — `""` is the
  resolver's canonical spelling. Do not invent a second spelling in one handler.

### 8.3 Deliberately absent

**There is no delete-store route or button.** Undeclaring a store orphans whatever it
remembered; that needs explicit operator direction, not a dashboard button. `POST
/api/memory/stores` (create) is additive and is present.

## 9. Test isolation — three barriers, and why each exists

**A subagent truncated the operator's live 36 MB `memory.db` to 29 bytes during this
work.** Mechanism: a test called `monkeypatch.undo()`, which reverts the *shared*
instance's whole stack including the rootdir `KIROCREW_HOME` pin;
`resolve_store_path("work")` then degraded to `"default"` and a deliberate
corrupt-file write landed on live data.

Three barriers in `conftest.py`, each closing a hole the previous one left:

1. A **private `MonkeyPatch`** instance for the home pin, whose undo stack no test's
   `undo()` can reach.
2. An **import-time `mkdtemp` floor** (`_HOST_HOME_FLOOR` + `atexit`, mode `0700`,
   random name) so "`KIROCREW_HOME` unset" is unreachable. The first attempt was a
   pid-predictable `mkdir(exist_ok=True)` at mode `755` — the exact
   symlink-pre-creation hazard the same file's `_create_tmp_root` docstring forbids;
   81 orphaned directories had accumulated.
3. `_refuse_a_real_data_home()`, called from the **single** `pytest_configure`,
   refusing a home **equal to, a parent of, or a child of** a real one. The first
   version used exact equality and let `$HOME/.kiro` (kiro-cli's own home) through
   with 16 tests collected. A second `pytest_configure` was also written first — a
   module defines one, and a duplicate **silently replaces** the earlier definition,
   so the guard was dead code. A test now pins that.

**Rules for anyone writing tests here:** never call `monkeypatch.undo()`; pin
`KIROCREW_HOME` to `tmp_path` in every test that touches a store; assert containment
immediately before any deliberate corrupt-file write.

## 10. What shipped

Backend: `memory_schema.py`, `memory_backup.py`, `memory_stores.py`,
`dashboard/handlers/memory_admin.py`, plus store scoping through
`dashboard/handlers/memory.py`, `context.py`, `history_consolidation.py`,
`vector_memory.py`, `security/`, `heartbeat.py`, the Slack/Discord/Telegram dispatch
paths, `learn.py`, `mcp_tools/spawn.py`.

**CLI** — five verbs under `kirocrew memory`: `backup`, `backups`, `restore`,
`retired` (`--restore <id>`, `--limit`), `carve` (`--scope --surface --crew
--session-key --derived-from --kind --count-by --store`).

**HTTP** — `?store=` on the twelve content routes, plus
`GET|POST /api/memory/stores`, `GET /api/memory/retired`,
`POST /api/memory/retired/restore`, `GET /api/memory/backups`,
`POST /api/memory/backup`, `POST /api/memory/restore`.

**Config** — `memory.backup_enabled` (`True`), `memory.backup_keep` (`7`).

**Nothing is behind Developer Mode or Feature Previews.**

## 11. Not done — pick these up

Ordered by how much they matter.

1. **Completed in the 2026-09-07 pickup: independent host-isolation undo stack.**
   Fifteen root host-floor fixtures now use `_host_floor_patch`, including shared Kiro
   paths, subagent paths, process guards and download/telemetry environment guards.
   Shared overrides unwind before the private floor at teardown, preventing stale
   per-test values from being restored over the ambient state. Mid-test shared undo
   and both teardown orderings are covered without invoking an unpinned operation.
2. **Retune the long-text admission relaxation** (§5): the constant claims `0.13`
   dilution, the corpus measures `0.079`. The benchmark exists; the constant was never
   changed.
3. **The retrieval contract is untouched.** The design doc's load-and-dump vs
   retrieve-on-demand context-injection question was never addressed; the retrieval
   path is unchanged.
4. **No upgrade path for pre-existing silos.** A silo created before this work stays on
   v1 forever. Only newly created silos get `memory_items`. Decide whether that is
   permanent.
5. **Seven remaining store-identity call sites** carry no crew identity, so reading the
   global store is correct there — the gate reports them as a pre-existing backlog.
   Its explicit allowlist is empty. Name their store deliberately when converting them.
6. **`memory_stores/` is invisible to snapshot, portability, and redaction.** A silo's
   contents are outside the export zip and the snapshot `memory` component. Its FTS
   index is fully derived so that part is only a rebuild, but the vector file is not.
7. **A silo is no longer fenced against a SHELL read** — see §11.1. Decide whether the
   sandbox alone is enough for a silo, or whether silo paths deserve a control the
   keystone does not have.

### 11.1 What the rebase changed about the silo threat model

Upstream removed path matching from `is_sensitive_bash_command` **deliberately and with
reasons** (four issue numbers in its docstring): a text matcher over `cat <fenced
path>` adds nothing on top of controls that cannot be talked around, and it denied
ordinary read-only commands whenever a fenced spelling appeared as *data* — a grep
pattern, a commit message, a note. Its docstring now states that **a keystone read
through the shell is permitted by design**, and points at the two controls that do the
work: the OS sandbox, and `is_sensitive_path` on every resolved path the file tools
open.

The consequence for memory v2: a silo path in **command text** is not refused. The
`memory_stores/` entry in `_CREW_SECRET_LEAVES` still fences the agent's **file tools**
(`is_sensitive_path` / `is_sensitive_write_path` both answer `True`), and the sandbox
still confines the process — but the shell text gate no longer helps.

`test/test_memory_stores.py::test_the_shell_text_matcher_deliberately_does_not_fence_a_store`
records this rather than asserting the old behaviour, so the boundary is written down
where someone looking for it will find it. **If that test starts failing because a path
matcher came back, that is a decision to make deliberately — with the false-denial
class in mind — not a regression to fix by making the assertion pass.**

Silos get exactly the same treatment as `~/.aws` and the governance keystone here, so
this is consistent rather than a silo-specific hole. Whether it is *sufficient* for a
silo is the open question in item 7.

## 13. File map

One commit: 143 files, +27,039 / −853, of which 30 are new.

### New backend modules

| File | Lines | What it owns |
|---|---|---|
| `src/kiro_crew/memory_schema.py` | ~805 | The crew lineage. `LINEAGE_V1`/`LINEAGE_CREW`, `CREW_SCHEMA_VERSION`, `CREW_SCHEMA_SQL`, `MIGRATIONS_CREW`, `detect_lineage`, the per-lineage relation/guard/insert builders, `MemoryFacets`, `FACET_NAMES`, `GROUPABLE_COLUMNS`, `FacetsUnsupported`, `UnknownFacet`, the shared `MEMORY_EVENTS_SQL`/`MEMORY_META_SQL`. |
| `src/kiro_crew/memory_stores.py` | ~635 | Store names → paths. The three resolvers (`memory_store_dir_for`, `resolve_store_path`, `memory_index_path_for`), the shape rule (`memory_store_name_defect`, `validate_memory_store_name`), the degrade cascade (`degrade_store_name`, `resolve_declared_store`), the positive predicate (`named_store_of_db`), the confirm pair (`owned_store_path`), `declared_store_names`, `ensure_memory_store_dir`, `warn_if_binding_degrades`. **Leaf module: stdlib-only at import time**, so `security` can depend on it. |
| `src/kiro_crew/memory_backup.py` | ~318 | Rotating hot backups. `backup_store`, `list_backups`, `prune_backups`, `back_up_all_stores`, `newest_backup`, `restore_from_backup`, `backup_dir_for`, `MemoryBackupFailed`. |
| `src/kiro_crew/dashboard/handlers/memory_admin.py` | ~816 | The seven new owner-gated routes. |
| `scripts/check_memory_store_seam.py` | — | The CI gate for memory-context call sites that name no store. Has a `--test` self-test that plants one probe per rule. |
| `src/kiro_crew/eval/bench/admission.py`, `admission_corpus.py` | — | The admission-threshold benchmark and its corpus. |

### Key modified backend files

`dashboard/handlers/_shared.py` (the request seam: `resolve_requested_memory_store`,
`markdown_memory_for_store`, `vector_memory_for_store`, `resolve_lesson_memory_store`) · `dashboard/handlers/memory.py` (twelve store-scoped routes +
`_vector_tier_for_request`) · `vector_memory.py` (lineage binding, facet stamping,
bounded recoverable retirement, facet reads) · `context.py` (`store_of_session`,
`session_store_for_turn`, `ContextBuilder.ensure_store`) · `security/paths.py` (the
`memory_stores/` keystone fence) · `security/__init__.py` (multi-store `scan_memory`) ·
`heartbeat.py` (the daily backup tick) · `config/sections.py` (the two config keys) ·
`history_consolidation.py` (`_session_facets`) · `cli.py` / `cli_commands.py` (five
verbs).

### Frontend

`website/src/pages/overview/MemoryStoreCard.tsx` is the module that owns the shared
store vocabulary the sibling cards import: `MEMORY_STORES_KEY`,
`MEMORY_QUERY_PREFIXES`, `memoryQueryRetry`, `memoryErrorCode`, `MemoryScopeNotice`,
`useMemoryStores`, `NO_MEMORY_STORES`. Start there.

Then `MemoryCarveCard.tsx`, `MemoryRetiredCard.tsx`, `MemoryBackupsCard.tsx`, and
`MemoryTab.tsx`'s internal `MemoryDocCard`. Client methods and types are in
`website/src/api/client.ts` and `website/src/types/index.ts`.

## 14. The HTTP surface

`?store=<name>` is optional on the twelve content routes. **Absent** → the global store
including `carve`. **Present** → owner-gated. Session headers cannot select a silo
through this content API; authenticated agent lessons have their own resolver (§7.5).

| Method | Path | Notes |
|---|---|---|
| `GET`/`PUT` | `/api/memory/preferences` | markdown tier |
| `GET`/`PUT` | `/api/memory/projects` | markdown tier |
| `GET`/`PUT` | `/api/memory/history` | markdown tier, today's file |
| `GET`/`PUT` | `/api/memory/semantic` | |
| `DELETE` | `/api/memory/semantic/{key:.+}` | |
| `GET` | `/api/memory/episodic`, `/api/memory/episodic/search` | |
| `DELETE` | `/api/memory/episodic/{id}` | |
| `GET` | `/api/memory/stats` | counts are per store; the embedding provider, migration flag and legacy-markdown probe stay INSTALL-wide |
| `GET` | `/api/memory/events` | |
| `GET` | `/api/memory/carve` | absent store means global; answers `{"store": ""}` for the global store |
| `GET` | `/api/memory/stores` | owner-gated, **no** `?store=`; returns `{stores, active}` |
| `POST` | `/api/memory/stores` | declares a store; `201` |
| `GET` | `/api/memory/retired` | |
| `POST` | `/api/memory/retired/restore` | registered BEFORE `/api/memory/retired` |
| `GET` | `/api/memory/backups` | never returns a filesystem path |
| `POST` | `/api/memory/backup` | returns `{backed_up, skipped, pruned, failed}` |
| `POST` | `/api/memory/restore` | returns `{ok, superseded}` |

Route order matters — `routes/memory.py` says so at the top: aiohttp resolves in
REGISTRATION order, and several routes rely on a literal path being registered before a
pattern that would swallow it.

**Every non-2xx body carries a machine-readable `code`** (backend strings have no i18n
catalog path). The ones this work introduced or relies on:

`owner_only` 403 · `unknown_memory_store` 404 · `store_unavailable` 503 ·
`facets_unsupported` 409 · `unknown_facet` 400 · `invalid_pagination` 400 ·
`invalid_episode_id` 400 · `unknown_retired_episode` 404 ·
`invalid_memory_store_name` 400 · `memory_store_exists` 409 · `config_unreadable` 500 ·
`memory_stores_unreadable` 500 · `invalid_backup_name` 400 · `backup_not_found` 404 ·
`backup_corrupt` 409 · `restore_failed` 500 · `restricted_session` 403.

`test/test_error_code_contract.py` pins that every new non-2xx body carries one.

## 15. Test inventory — which file pins which invariant

~11,300 lines of new tests. When you change something, this is the file that will tell
you:

| Test file | Pins |
|---|---|
| `test_memory_v1_golden.py` | **The acceptance proof.** The default store's tables, rows, ranking and on-disk footprint are unchanged. Never edit to make it pass. |
| `test_memory_v2_schema.py` | The two lineages, `detect_lineage`'s structural rule, the views' column order, that facets are unreachable from every ranker, and that a pre-existing silo keeps v1. |
| `test_memory_lineage_drift.py` | That no statement drifts between lineages. |
| `test_memory_v2_facet_read.py` | The carve read side: filters EXCLUDE, hostile axis names are refused AND the table survives, the two refusals, and the route's owner gate. |
| `test_memory_v2_isolation.py` | One store's writes never reach another, across the markdown, vector and lessons tiers. |
| `test_memory_stores.py` | The name shape rule, the two failure postures, the symlink aliasing refusals, the keystone fence, and (now) that the shell text matcher deliberately does not fence a store. |
| `test_memory_store_seam.py` | The call-site backlog ratchet — it goes red if a new memory-context call site names no store. |
| `test_memory_store_dashboard.py` | The owner gate, the cross-module `request["user"]` property, the absent-parameter refusal of `X-Session-Key`, isolation through the API, the 503, the backup-name containment, and that one damaged store does not hide the healthy ones. |
| `test_memory_backup.py` | Consistency under a live writer, the three outcomes, the interval guard, atomicity, non-destructive restore. |
| `test_episodic_retirement.py` | The cap, the containment requirement, the probe, recoverability. |
| `test_scan_memory_stores.py` | That the credential scan walks every declared store and reports an unauditable one instead of rounding to clean. |
| `test_home_pin_survives_monkeypatch_undo.py` | That `monkeypatch.undo()` cannot lift the home pin, and that the guard is reached from the ONE `pytest_configure`. |
| `test_episodic_admission_bench.py` | The benchmark's protocol, not a bare F1. |
| `test_security_facade.py` | That every submodule-owned re-exported name is in the frozen manifest. |
| `website/src/test/MemoryStorePicker.test.tsx` | The picker's listing, that a switch re-reads every card under the new name, `facets_unsupported` rendering, the two-click restore, the `owner_only` message. |
| `website/src/test/OverviewMemoryTabCov80.test.tsx` | The tab's own write paths, including that a storeless save sends `undefined` rather than `store=default`. |

## 16. Recipes

### Add a store-scoped route

```python
store, denial = await resolve_requested_memory_store(request, state, "thing.read")
if denial is not None:
    return denial
tier = await vector_memory_for_store(state, store)      # or markdown_memory_for_store
if tier is None:                                        # silo only
    return _store_unavailable_response(store)
```

An absent parameter always means the global store. Do not reintroduce a session-binding
escape hatch here. Agent lessons use `resolve_lesson_memory_store` with its separate
positive authentication requirement; see §7.5.

### Add a carve facet

It is a data change, not an evaluator change:

1. Add the field to `memory_schema.MemoryFacets` (`FACET_NAMES` derives from it).
2. Add the column to `CREW_SCHEMA_SQL` **and** a migration in `MIGRATIONS_CREW` with a
   new version number.
3. Stamp it in `history_consolidation._session_facets` if a session can supply it.
4. Add the CLI flag in `cli.py` (argparse cannot build flags from a runtime tuple
   without paying an import per invocation, which is why that list is the one hand-kept
   copy — `test_memory_v2_facet_read.py::TestTheAllowlistIsDerivedNotRestated` is what
   keeps it from drifting into a silently unreachable axis).
5. Do **not** add it to either compatibility view.

### Add a store-aware frontend card

Import the shared vocabulary from `MemoryStoreCard.tsx` (`useMemoryStores`,
`MemoryScopeNotice`, `memoryErrorCode`, `memoryQueryRetry`, `MEMORY_QUERY_PREFIXES`).
Put the store in the React Query key, mount it from `MemoryTab` with `key={store}`, and
add the new English literals as plain strings — then run the i18n pass over all 13
catalogs with **real** translations (`[changed-passthrough]` is zero-tolerance).

### Verify a change against a running gateway

`docs/guides/worktree-verification-recipes.md`. Never point a harness at `~/.kiro/crew`.

## 17. Correction log — claims that were revised

Recorded because each was stated confidently before being checked, and a following
session should not rediscover them from the earlier reports.

| Claim | Correction |
|---|---|
| "49.8% of episodic rows were irreversibly tombstoned" | Wrong. The rows are physically present with full text; only `memory_events` takes a hard `DELETE`. |
| "The admission F1 of 0.980 does not reproduce (0.721)" | Wrong as stated. It reproduces at 0.976 under 1:1 balance; the real finding is that the protocol cannot have selected 0.55, and that realistic balance gives 0.360. |
| "`CONTRACT_VERSION` is the memory schema version" | Wrong. `CONTRACT_VERSION` is the PlatformContext composition contract; the memory schema version is unrelated. |
| "An absent `?store=` is byte-identical to what every route did before" | Wrong, and it was a live escalation. Eleven routes served the GLOBAL store before; resolving the binding was new reach. See §7.5. |
| "The memory-store fence survived the security package split" | Wrong. It was lost and had to be re-applied to `security/paths.py`; `test_memory_stores.py` caught it. |
| "The handoff covers the memory v2 UI" | Too narrow. The work is all of memory v2; this document covers the schema, backups, retirement, facets, isolation, CLI, security scan and UI. |

## 18. State at the original push (historical)

Pushed to `origin` (`kirodotdev/KiroCrew`) as branch **`feat/memory-v2-ui`**, one commit,
rebased onto `origin/main`. No pull request has been opened.

Verified at push time: `test_memory_v1_golden.py` passes unedited; 921 backend tests
across the memory suite; 25,537 frontend tests across 1,602 files; mypy clean on 1,325
files; flake8, isort, black, subprocess-encoding, docs-lint, harness-parity, brand,
feature-map and memory-store-seam all green; 19/19 i18n checks against `origin/main`.

Two known gate failures that were not this branch's to fix at push time:
`scrub-lint.sh` on a pre-existing non-ASCII fixture in
`test/test_atomic_write_named_duplicates.py`, byte-identical to `origin/main`; and
`check_changelog_history.py` failing only with `CHANGELOG_BASE_REF=origin/main`
while the branch was behind (`CHANGELOG.md` is byte-identical to the merge-base
and no branch commit touches it — the release PR writes that section).


## 19. Pickup on 2026-09-07

Fetched `kirodotdev/KiroCrew` and checked out the actual remote branch
`origin/feat/memory-v2-ui` at `9bfce861ee81e0176f2c1935585dbc6e51cee90d`
in a separate worktree. The original checkout is untouched. At pickup,
`origin/main` was `2847b5b2d` and was 68 commits ahead of this branch's merge
base; this worktree is one feature commit ahead. Main has not been merged or
rebased into it.
At the final status check the shared `origin/main` ref had advanced to `678fc3266`
(71 commits ahead of this branch's merge base). The feature branch still exactly
matches `origin/feat/memory-v2-ui`; the pickup changes are in its working tree.

The first implementation pass addresses three safety boundaries:

- The remaining shared host-floor patch stacks (§11 item 1).
- Unverified session headers selecting silo contents: carve's absent-store escape
  hatch is removed, and all three lessons routes require positive authorization
  before following a named binding (§7.5).
- A pending or failed markdown document read can no longer enable editing or Save.
  Its empty placeholder could otherwise overwrite a real document. Successfully
  loaded empty documents remain saveable, and typing during an in-flight save still
  survives its completion.

Further review found a benchmark reporting defect: `f1_band` returned the best
threshold twice even when no threshold reached the requested F1 floor. It now
returns `None` in that case, exposed as JSON `null` and an explicit not-reached
message. Both regression cases failed before the fix; the instrument suite now
passes 19 tests with the model-dependent test skipped. No threshold or historical
measurement was changed. Retuning still needs a fresh model-backed measurement.

The authoritative memory, dashboard, security and testing specs are updated alongside
these changes. All pickup changes are local and uncommitted; no push or PR was made.
The original push-time test totals in §18 are historical, not evidence for this pickup.

Review coverage: the full handoff and all 143 files in the feature diff have now been
read, including the complete added tests, locale changes and ancillary specifications.
The core context/vector changes were re-read in bounded diff slices to close earlier
truncated-output gaps. This is a source review and the verification below, not a claim
that every runtime configuration has been exercised or that the whole memory-v2 vision
is done.

The locale review found false scope claims in the crew editor and member drawer:
scheduled jobs still use global memory, channel isolation requires the recorded session
binding, and multiple agents may intentionally share one named store. All 12 real locales
now state those limits, and the non-owner picker notice correctly says that unselected
memory documents read the global store. The pseudo-locale is regenerated from English.
The carve, retired and backup cards also have distinct card-and-store React keys;
the prior shared key emitted duplicate-sibling warnings during the picker tests.
Switching stores still remounts all three and drops any armed restore confirmation.

The remaining design work in §11 is still open. Additional implementation follow-ups
identified while reading: `spawn_continue` recovers `memory_store` only from the live
run record, so restart persistence needs an explicit contract; and the three markdown
editors still use the global vector card's migration visibility, so a selected silo's
editable documents can be hidden by another store's state. Address these with the
persistence and UI scope work, keeping the default golden file unchanged.

### Pickup verification

- 100 frontend tests passed across the memory picker, editor, integration and roster
  suites. Both initial-read save regressions failed before the fix and passed after it.
- TypeScript, the production build, ESLint on the changed components/tests and all
  19 i18n checks passed. The scope notice was corrected in all 12 real locales and
  the pseudo-locale was regenerated. The final i18n rerun passed all 19 checks after
  the crew/member scope corrections. The latest production build and changed-file
  ESLint checks also passed. All 100 frontend cases passed across the final run and
  the 61-test roster rerun after updating the old-copy assertions; the duplicate-key
  warnings are gone.
- The final lessons/isolation rerun passed 183 tests, covering owner/internal access
  and refusal of non-owner dashboard and App Kit requests on all three lesson verbs.
- A Linux run of golden, crew routing/delegation and config tests passed 559 tests
  with 4 skips. The golden file is unchanged. Windows needs symlink privileges for
  several existing cases and has two tests expecting POSIX path separators; these
  pass in Linux without weakening those assertions.
- Host-floor verification passed 15 focused tests and the expanded Windows host-guard
  checks. The WSL copy of the Windows worktree cannot resolve its Windows `.git`
  pointer with native Linux Git; two Git-residue checks from the separate host-guard
  run therefore fail there. Those checks pass in Windows.
- Black's repository ratchet, changed Python flake8/isort checks, subprocess encoding,
  docs lint, harness parity and the memory-store seam gate passed. The last gate still
  reports the seven pre-existing call sites from §11; its explicit allowlist is zero.

Logs and local build artifacts are kept under the worktree's ignored `.venv/` and
`website/dist/`. No operator memory database was opened for testing.

Final expanded Linux run: **3,305 passed, 10 skipped**, with two query-plan assertions
and four native-Git setup errors remaining. All six were resolved and rechecked in an
**11-passing-test** targeted run (the seven planner cases plus the four Git cases):

- SQLite 3.45.1 may satisfy ordering by SCANNING `idx_mi_created`; SQLite 3.53.1
  chooses a different scan. Neither provides a facet seek. The test now requires
  `SCAN memory_items` and excludes `SEARCH memory_items`, preserving the intended
  complexity assertion across both versions. All seven planner cases also pass on
  Windows. No query or index was changed.
- For the four read-only Git inventory tests only, WSL received explicit native
  `GIT_DIR` and `GIT_WORK_TREE` paths for this existing worktree. Do not export those
  globally or for tests that create temporary Git repositories. No Git metadata was
  changed to make the Linux tests work.

This covers 3,311 distinct passing backend cases across the expanded run and its
rerun, with 10 skips; it is not a claim that the entire repository test suite ran.
The unedited default-memory golden cases are included. Remaining work is the design
and implementation scope from §11/§19, not an unresolved failure from this validation
pass. The later benchmark fix passed its own 19-test suite (one model-dependent skip).


## 20. Member-isolated V2 implementation and visual experience

This section supersedes the implementation gaps and member-sharing descriptions
in §11 and §19. The user's accepted product boundary is now explicit: **Global
Memory remains V1; each named Crew Member owns private Memory V2.** There is no
V1 migration in this implementation. The branch and separate worktree from §19
are retained; the original checkout has not been changed. These changes remain
local and uncommitted, with no push or pull request.

### Identity and end-to-end behavior

A member is published only after its unique empty store and ownership manifest
exist. The private binding is immutable; a second member cannot share it.
Existing members have an explicit Initialize action that starts empty and keeps
old data. The member editor links directly to its memory management view instead
of offering a shared-store selector. The reserved default assistant retains V1.

| Entry point | Resulting experience |
|---|---|
| Ordinary chat without a named member | Existing Global Memory V1 behavior |
| Member private chat without Crew Mode | That member's private V2 is read and written |
| Crew Mode | Each named delegate receives its own V2; explicit task/result handoffs carry the shared context |
| Scheduled work | Member identity is distinct from provider-template identity and pins the member store |
| Restart, retry and continuation | Protected persisted identity restores the original binding before transcript data is consulted |
| Missing, corrupt, mismatched or unreadable memory | The task reports a concrete failure before provider execution; no global fallback |

Channel hydration, native messaging, scheduled result injection and consolidation
follow the same binding. Private consolidation does not export a member's
experience into globally available automatic skills. Owner-only management may
inspect declared stores; member-facing MCP recall resolves the trusted session
and provides no caller-selected store override. Existing unowned background work
continues using V1; converting that work into a member is a separate explicit
binding decision.

### Retrieval, correction and recovery

The original V2 snapshot for positively owned databases used hybrid semantic and
lexical retrieval, CJK-aware Unicode terms, bounded age/importance scoring, relevance
admission before limiting/MMR, recoverable retirement of explicit contradictory
assignments, and exact full-content duplicate handling. Missing embeddings do
not hide matching lexical evidence. Semantic JSON is decoded before lexical
matching and prompt rendering. The global V1 golden file remains unchanged.

The short/long cosine operating points are provisionally 0.62/0.57, selected from
the previously committed model-backed corpus. A reproducible complete hybrid
evaluation now runs the real V2 SQLite candidate scan, ranking, MMR and bounded
recall using the already-installed Qwen3 GGUF. No model was downloaded and no
live provider/answer-generation benchmark was run. Its 50 topics, 100 fragments
and 5,000 labelled pairs per mode remain corpus-informed evidence rather than
held-out calibration or a production-quality guarantee. Future tuning and V1
migration remain deliberate later work, as requested.

| Retrieval mode | Admission precision | Fragment recall | Context topic hit | Context nDCG@8 |
|---|---:|---:|---:|---:|
| All vectors present | 93.94% | 93% | 98% | 0.9274 |
| All 50 short-fragment vectors missing | 92.41% | 73% | 96% | 0.7714 |
| No embeddings | 91.80% | 56% | 74% | 0.5962 |

Full-vector context macro precision was 93.67%; macro precision counts queries
with no returned memory as zero, whereas admission precision pools all pairs.
All 900 cap checks (six caps × 50 queries × three modes) passed. Chinese,
Japanese and Korean keyword-only fact/episode fixtures, oversized-row skipping
and forgotten-row exclusion passed; these are structural checks, not a
multilingual benchmark. Reproduce with `python -m
kiro_crew.eval.bench.member_v2 --model-path <existing.gguf> --json <report.json>`.
The source artifact `src/kiro_crew/eval/bench/data/member-v2-hybrid-qwen3.json`
contains corpus and model SHA-256 identifiers and selected per-query evidence.

Private context uses bounded preference/project anchors and relevant recall
instead of dumping daily history. `memory_recall` supports later topic changes;
its result includes source and relevance evidence. Correcting a rule preserves
its structured metadata, and structured facts require valid JSON. Forgetting
removes future long-term recall without rewriting an active conversation.
Owner-only episode PATCH correction preserves id, creation time and copy
provenance, records before/after audit atomically and invalidates stale vectors.
Identical retries are no-ops, duplicate conflicts and invalid edits refuse
without writes. Source-identity tracking also prevents a repeated copy request
from undoing an owner correction or resurrecting a forgotten imported episode.

Copying starts with no selected rows. The owner explicitly chooses up to 50
source items; the server validates the whole selection before writing and
records source store, identity, kind and time. It never overwrites an existing
or tombstoned destination identity. A mid-batch storage failure retains confirmed
outcomes, marks the uncertain item `unconfirmed` and remaining items
`not_attempted`; the UI displays the partial result without claiming rollback.
Malformed selections fail before any write.

V2 backups include a consistent database snapshot, preference/project documents,
history and lessons with a checksummed ownership manifest. They live outside the
active member folder so recovery remains possible if that folder is lost.
Restore validates the same member/store and stages a replacement; the UI and CLI
state that a restart is required and current memory has not changed. Gateway
startup applies it before opening memory. A journal and retained recovery copy
cover interrupted installation and rollback. Global V1 backup behavior remains
unchanged. Whole-install export/snapshot portability from the original §11 is
separate from this completed per-member backup and restore lifecycle.
Pending backup status is returned by GET across remounts. The owner can cancel
a staged restore through the dashboard or `memory restore --store <store>
--cancel-pending`; active memory and the original backup are preserved. A
second restore cannot replace pending state until it is explicitly cancelled.
Once activation has displaced a tree, cancellation refuses so startup can
complete crash recovery. Private snapshots use microsecond+UUID names so taking
two backups in the same second cannot overwrite the earlier one.

### Management UI

The canonical route is `/settings/overview?view=memory&store=<store>`. The private
view does not mount global settings or migration controls. Its compact identity
header, the owning member's avatar with a glowing lock badge, tinted category icons, three-line previews,
responsive card grid and subtle reduced-motion-aware transitions replace the
previous wall of text. Full content, readable provenance, correction and forget
actions live in focused dialogs.

The avatar is the same member identity shown by the roster: exact member name
plus its configured ghost or uploaded picture. It appears consistently in the
header, store picker, copy source and provenance. The store listing supplies
`owner_avatar` alongside `owner_member`; changing the member avatar updates that
presentation without changing the private store binding. Global V1 uses a
separate database icon.

Memories, Profile and Recovery have separate tabs. Search queries the entire
selected store before pagination, using literal Unicode-normalized matching;
the browser does not discard valid server matches with a second text filter.
Visited sections preserve drafts. Store switching, app navigation and browser
unload protect unsaved work. Recovery remains accessible when the active store
is unavailable. Copy results, failed reads and pending restore status report
what actually happened. All new copy is translated into the 12 real locales;
the pseudo-locale is regenerated.

Current owning contracts are in the memory, config, session, history, Crew Mode,
subagent, messaging, Slack gateway, dashboard and CLI module specifications,
plus the MCP inventory and website narrow-viewport contract. The sandbox and
resolved file-tool fences from §11.1 remain the shell/file trust boundary; this
implementation does not restore the removed shell-command text matcher.


### Verification of the member implementation

These results belong to this implementation, not the historical totals in §18
or the first pickup totals in §19. Overlapping suites are not added together.

- The completion audit's focused Windows lifecycle/algorithm/backup/lineage and
  evaluation-integrity run passed 131 cases. It covers atomic episode correction,
  provenance-aware copy retry, persistent restore status, cancellation and old/new
  snapshot timestamp compatibility. Log: `.venv/member-lifecycle-complete.log`.
  The corresponding final Linux run added all 21 unchanged V1 golden cases and
  server search: 176 passed (`.venv/linux-member-lifecycle-complete.log`). The
  separate existing dashboard store-administration regression passed 28 cases;
  CLI help confirms `--from` and `--cancel-pending` are mutually exclusive.
- A real WSL standard-namespace kernel probe used an isolated temporary data
  home. The member binding was readable; changing it, creating a forged child
  and renaming its parent were denied. A sibling private DB created by the host
  only after the sandbox process was ready remained unreadable. The absent
  private root was materialized before mounting. Probe script:
  `.venv/member_sandbox_probe.py`; these are kernel results, not string assertions.

- The 52-file expanded Linux memory run collected 2,231 cases: 2,172 passed,
  22 skipped, 33 failed and four Windows-worktree Git setup errors. All failure
  groups were corrected and rechecked; no assertion was weakened to permit
  private-to-global fallback. Its log is `.venv/linux-memory-expanded.log`.
- The final 22-file Linux regression passed 1,189 cases with 12 skips. Its one
  remaining linked-member fixture failure was isolated to paired Slack hydration
  caches and fixed; the complete member runtime file then passed all 23 cases.
  Logs: `.venv/linux-memory-final.log` and
  `.venv/linux-member-runtime-final.log`.
- The final narrow Linux run passed 166 cases: V2 algorithms including decoded
  Chinese JSON, all 21 unmodified V1 golden cases, member APIs, lineage, member
  turn context and Unicode list search. This includes malformed seed-kind and
  partial-copy tests. Log: `.venv/linux-memory-unicode-final.log`.
- Four read-only Git seam cases passed using translated WSL worktree paths only
  for that process (`.venv/linux-memory-seam-final.log`). Do not export this Git
  override for tests that create temporary repositories.
- The member ownership/config/dashboard Windows run passed 258 cases with three
  platform skips. Its member lifecycle tests use isolated real databases, not
  operator memory.
- The final seven-file frontend integration run passed 179 cases, including
  private store identity, scoped writes, copy outcomes, drafts, roster behavior
  and server Unicode matches in both memory and copy views. Shared shell and
  navigation changes passed their separate 68-case suite.
- Browser QA exercised English and Chinese views, copy/correction dialogs,
  Profile and Recovery at 320, 390 and 1280 pixels. There is no document-level
  horizontal overflow; recovery tables scroll within their card. The isolated
  browser fixture mocks API responses and is not a live gateway/provider E2E
  run. Real storage/runtime behavior is verified by the backend suites above.

Screenshots and the interaction recording are under
`.venv/member-memory-visual/`, including `zh-memory-1280.png`,
`zh-memory-390.png`, `zh-memory-320.png` and `walkthrough.webm`.
The production frontend artifact is `website/dist/`. No operator memory was
opened or migrated for verification.


The subsequent avatar identity pass keeps every private store visually tied to
its member, including uploaded-picture revisions and pinned ghost traits.
Its nine-file frontend regression passed **196 cases**, including the shared
select's desktop and native touch behavior. The backend owner-avatar projection
and update-without-rebinding check passed with the ownership/dashboard suite
(**53 cases**). These overlap the earlier counts and are not additional totals.
The latest visual artifacts are `avatar-memory-1280.png`,
`avatar-memory-390.png` and `avatar-picker-1280.png` under the same
`.venv/member-memory-visual/` directory; the picker shows two distinct member
faces alongside the separate Global V1 entry.

Final TypeScript/production build, changed TypeScript ESLint, mypy on 43 changed
production modules (plus the avatar endpoint recheck), changed Python flake8 and
isort, the Black ratchet and the touched avatar Python formatting checks all
passed. All 19 i18n checks passed against `origin/main`; documentation lint,
harness parity, brand, feature map, subprocess encoding and memory store seam
checks passed. The seam report retains the pre-existing unowned V1 sites with
an empty explicit allowlist. The build retains its existing large-chunk warning;
no new provider benchmark or live gateway verification is implied by these gates.

### Completion audit: private execution and caller identity

Private V2 runs require Crew's enforced Linux/WSL namespace or macOS outer
Seatbelt filesystem sandbox. The guard follows the spawn layer's governance
floor and the actual member-DM backend choice. The persisted enablement value
is `agent.sandbox=auto`; explicit `off`, unavailable namespace/Seatbelt support,
and macOS Kiro internal delegation refuse before private provider preparation
or consolidation. Native Windows member execution refuses with WSL guidance;
native owner dashboard lifecycle management and Global V1 remain available.
Errors identify the actual unavailable mechanism and remedy.

Protected run memory records now live at
`member-memory-bindings/<run-id>/memory.json`; protected process records are
`member-memory-bindings/pids/<pid>.json`. This top-level directory is precreated
and mounted read-only. Old writable `trust/` memory records are not accepted as
migration authority. Corrupt/unreadable identity never becomes Global V1.
The private database root is precreated and hidden before sandbox spawn, so
creating a new member later cannot expose it inside an older sandbox.

Internal private recall, lesson reads/writes and consolidation require positive
kernel process/session attribution or a short-lived proof issued by the trusted
MCP gateway for that process. A shared local secret and forged session header
are insufficient. The proof signing key is hidden under `memory_stores`; proof
expires and is invalidated by process reuse or session rekey. A proof for one
member cannot authorize another session or a consolidation body naming it.
Private ACP clients use direct MCP servers inside their member sandbox. They
discard shared broker overlays and sockets before all create, reload and resume
paths. The private filesystem boundary withholds shared broker endpoints and
aliases, including older daemons without member verification. V1 retains pooling.
Trusted proxy backends receive proof only in current-call trusted metadata.

Private execution requires direct MCP support. The current public Codex ACP
backend is refused before startup. Select a supported member backend for private
chat and a supported default backend for Crew tasks and private consolidation.
V1 Codex remains unchanged. Configured broker endpoints outside the reserved
hidden namespaces also refuse private startup with a reason; arbitrary project
directories are never hidden to accommodate such an endpoint.

A subsequent downgrade audit closed the opposite direction too. A private
process cannot omit its header/proof or claim a legacy/global session to read,
write or consolidate V1. The HTTP gate compares the target against the private
store in its protected process record; positive unowned kernel callers retain
V1, and pure V1 installations retain their prior internal contract. Pooled MCP
refuses a missing/mismatched protected caller before forwarding. Private PID
records are not published for V1 shared sessions, preserving global tab rekeys.
The local-secret owner-token mint also refuses private or unverifiable callers
when member boundaries exist, while an actual unowned host app/CLI succeeds.
The login-link CLI must run on the gateway host (inside WSL for a WSL gateway)
so the kernel can attribute it. The resulting link still works in a browser
on another host; Windows-to-WSL mint requests correctly refuse unverifiable
process identity.

First trusted preparation now permanently pins a private session at
`member-memory-bindings/sessions/<sha256-session-key>/memory.json`. Mutable
transcript metadata must agree after restart: deleting its memory field,
selecting Global V1 or another member, or removing the protected file all refuse.
The trusted provider factory derives the member-specific sandbox flag from this
identity. Provider/client/runtime recovery retains it; caller kwargs and env
cannot override it. Private consolidation has an ephemeral member-bound provider
and releases/removes it after the turn, preserving the shared V1 background path.
Private MCP discovery reads protected process ancestry before legacy flat PID
files, allowing the member sandbox to hide future Global V1 files safely.

This later audit passed **295 Windows cases with six platform skips** and
**301 Linux cases**, covering auth, owner bootstrap, session/run persistence,
consolidation and opposing V1 behavior. Logs:
`.venv/member-pinned-identity.log` and `.venv/linux-member-pinned-identity.log`.
Mypy passed 13 production modules; flake8, isort and whitespace checks passed.
An earlier expanded provider/auth pass had 315 successes and one platform skip;
these overlapping counts are separate verification, not a sum.

The final focused auth/runtime regression passed 70 cases on Windows and the
same 70 on Linux, including macOS backend-selection cases and pre-provider
refusal with V1 preservation. Earlier focused API/isolation and PID signature
suites were separately checked; these overlapping counts are not added.
Logs: `.venv/member-http-runtime-ready.log` and
`.venv/linux-member-http-runtime-ready.log`. The new auth module passed mypy,
Black and flake8; changed runtime files passed `git diff --check`.
The actual isolated WSL gateway config was updated through the locked config
writer to `sandbox=auto`, and its private execution predicate returned true
before the final gateway restart. The kernel filesystem probe described above
supplies the enforcement evidence; the predicate alone is not that evidence.

### Completion audit: real gateway verification

The completion audit uses the separate worktree's production frontend build and
an actual WSL gateway with an isolated temporary home. The packaged fake ACP
backend avoids external provider calls; browser requests, member configuration,
SQLite data, ownership manifests and backup files are real. The earlier mocked
visual fixtures above are not the evidence for these flows.

The owner lifecycle flows cover selected V1 copying with provenance,
correction/reload/forget, unchanged V1 and sibling memory, persisted restore
staging and cancellation, and opening the exact empty member's conversation
with the same saved slot after reload. The three checked-in scenarios are in
`website/playwright/member-memory.spec.ts`; the isolated CI harness's executed
floor is 223. Desktop and 390px screenshots are produced by the first scenario.

The identity-loading audit additionally covers the initial catalog request:
private content waits for the owning member's identity, with a skeleton while
loading and an explicit retryable error on failure. A raw store UUID and generic
database avatar never stand in for a member. A known cached owner stays mounted
during background refresh errors so open edits are preserved. The latest focused
identity/UI suite passed 76 cases; all 19 i18n gates passed for the 12 real locales
and pseudolocale.

An additional actual process-restart check created two private members, backed
up one, changed its live memory and staged that backup. The original gateway
process was stopped and a new gateway started against the same temporary home,
without reseeding. The target's original memory returned, its pending state
cleared, and both the sibling and complete V1 list remained identical. Evidence:
`.venv/real-memory-e2e/restart-result.json`; driver:
`.venv/check_memory_restart.py`. This verifies activation, beyond a staging API
response or a browser reload. It does not claim a real model-generated turn.

Fresh frontend lifecycle regressions passed 206 cases and the focused
navigation suite passed 60. Pooled MCP caller/forwarding regressions passed 353;
all 19 i18n checks passed with explicit `origin/main` comparison. The native
Windows kernel test verifies IPv4 and IPv6 TCP caller PID attribution against a
real child process, while Linux platform regressions passed 565 cases with 25
platform skips. These overlapping counts are separate evidence, not one total.

The final combined restart also ran an actual member DM through the production
gateway and ACP allocator. The packaged fake provider produced the complete SSE
reply; its live protected PID, immutable session record and transcript all named
the same private store, and the complete fixture V1 semantic list was unchanged.
Evidence: `.venv/real-memory-e2e/private-member-turn-result.json`. This exercises
provider startup and allocation after the private pool/sharing bypass, without
claiming model reasoning quality or making external provider calls.

An actual production `mcp-core` stdio process then ran inside the private Linux
namespace with no session environment variable. Trusted preparation and protected
PID ancestry supplied its identity. `learn_add`, `learn_list` and `memory_recall`
completed against its own store. Direct requests claiming V1, a blank session or
another existing member were refused; a sibling store selector and owner-token
promotion were also refused. V1 and sibling semantic rows stayed identical.
CLI logs and local SEL writes succeeded in the isolated execution log view.
Evidence: `.venv/real-memory-e2e/private-mcp-result.json`; driver:
`.venv/check_private_mcp.py`. The recall route is included in the mixed transport
authentication bucket while retaining its member proof check in the handler.

All three browser scenarios passed again after that restart (four passes including
authentication setup). The phone check waits until all four filter labels are
fully inside the viewport, rather than capturing their layout transition from the
desktop position. Verified real screenshots are retained at
`.venv/member-memory-visual/real-memory-1280.png` and `real-memory-390.png`.
The final pool/sharing/caller suite passed 145 cases with two platform skips;
the filter/member panel suite passed 49. These runs overlap earlier coverage.

### Completion audit: member filesystem access to Global V1

Private provider execution now carries a gateway-resolved `private_memory` flag
through the actual spawn. The wrapper itself refuses off, unconfined, nested or
internal-delegation paths that cannot establish the private boundary. Global V1
provider behavior remains unchanged. Linux withholds global memory through a
namespace-owned view of the data-home and administrative workspace roots;
existing loose files are readonly for that provider's lifetime, and new loose
root files require provider recreation. Existing project child directories remain
live and writable. Protected identity and listener directories also stay live
for publication after the process starts. Shared broker endpoints are withheld
from private execution. This covers global DB replacement,
WAL/index files, temporary and superseded copies, global lessons, markdown memory,
backups, inherited cwd and aliases to these paths.

Private CLI and process-local SEL diagnostics persist under
`memory_stores/.execution-logs/member-<random>/`, hidden from both other members
and the default V1 assistant. Linux binds only that execution directory at the
child's `agent-logs/` before hiding the source root. A readonly namespace marker
selects the local diagnostics before PID identity publication; the selector also
requires a kernel-confirmed readonly filesystem, so a marker planted on the host
cannot redirect gateway logs or audit. Outer Seatbelt limits its diagnostic path
hint to the corresponding execution directory. Local diagnostic chains provide
no gateway/session authority; the gateway keeps recording API mutations itself.

The expanded sandbox/logging/SEL regression passed **965 Linux cases** and
**642 Windows cases with 325 platform skips**. After the hidden log backing and
marker validation refinements, **63 targeted Linux cases passed again**, including
real namespace execution, durable CLI and synchronous SEL writes before PID
publication, unchanged V1 global read/write, sibling log denial for both V1/V2,
and refusal of a planted marker. Five production modules passed mypy; flake8,
isort and whitespace checks passed. Evidence:
`.venv/member-global-fs-linux-final.log`,
`.venv/member-global-fs-windows-final.log`, and
`.venv/member-private-fs-final.log`. These are overlapping runs, not one total.
Seatbelt predicates are covered by profile tests; no macOS kernel was available
for this verification.
