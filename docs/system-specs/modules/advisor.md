# Advisor Module

## Overview

The advisor module (`kiro_crew/advisor/`) is an opt-in, asynchronous,
cross-model session reviewer. When enabled for a session, it observes the
primary agent's work at host-owned checkpoints, reviews the host-supplied
observation on an isolated reviewer session with no callable tools, and
returns severity-aware advice. It never impersonates the primary agent, never
blocks the primary turn, and is fully inert when disabled: no reviewer process,
no event buffering, no model cost, no UI noise.

It is a native session service owned by dashboard/session lifecycle code,
keyed by the effective parent session identity. It is not an app, not a peer
session, not a dashboard slot, and not an ordinary subagent.

Related feature requests: serial artifact review and iterate-until-clean, and
a clean-context review gate for final responses. The advisor implements the
asynchronous, opt-in, checkpoint-observing contract; those workflows can
later compose on the same subsystem.

## Package layout

| File | Responsibility |
|---|---|
| `observation.py` | Normalized, bounded, redacted primary event records; checkpoint batching; observation epochs. |
| `service.py` | Parent-session registry, effective config, enable/disable, lifecycle attachment, reset/dispose, the async review pump. |
| `runtime.py` | Shared reviewer runtime pool: per-parent reviewer sessions, crash self-heal, release/reap/shutdown (PID protection is `AcpRuntime`'s own). |
| `output.py` | Strict reviewer envelope validation (`AdvisorNote`). |
| `guard.py` | Emission guard: dedupe, non-blocker budget (4 per update, held by the dispatch that owns the update so overlapping dispatches cannot reset or spend each other's), interruption cooldown (120 s), interruption cap (3 per epoch; a blocker past it is preserved as a card, never steered; the slot is taken when the check passes, before the steer is awaited, and returned only on proven non-delivery (preserved, discarded, or revoked before any text landed); a steer that landed before authorization was withdrawn, or an unknown outcome, keeps the slot and starts the cooldown, so overlapping deliveries cannot exceed the cap) -- module constants; reset with the epoch. |
| `delivery.py` | Advisory envelope over the shared steer ledger; preserve-on-unconsumed; staged next-turn context. |
| `composition.py` | Dispatcher policy, prompt rendering, packaged-agent materialization, runtime factory over the agent SDK. |
| `usage.py` | Reviewer usage attribution helpers. |

## Observation contract

Checkpoints derive from facts the host owns — never from transcript polling
(edits, rewinds, regeneration, compaction, forks, and transfers make JSONL
polling unsafe):

1. Completed tool results coalesce (in order) into an `in_progress=True`
   `ObservationUpdate`.
2. A text segment finalized before a tool group is included in the next
   in-progress update.
3. The turn terminal produces exactly one `in_progress=False` final update
   per epoch; completion is idempotent, so a replayed terminal cannot emit a
   duplicate. A host-fabricated terminal carries `synthetic=True` and is
   never treated as a genuine provider completion.
4. Every update carries the parent session key, parent turn identity, the
   observation epoch, and a monotonically increasing sequence.

Records are bounded to `OBSERVATION_PAYLOAD_MAX_CHARS` characters (truncation
is flagged, not silent) and pass through the owner-supplied redactor before
buffering. Reasoning content is never written into the parent transcript for
the advisor's sake.

Neither the primary model's reasoning nor the user's messages are observed:
the records are the assistant's visible text segments and tool results only,
so the observer never holds anything rawer than the transcript surface and
no user-authored text reaches the second model. The reviewer therefore
judges the work on its own terms (wrong logic, risky actions, claims the
tool results contradict, changes that break the surrounding code) and goal
or requirement review is scoped out of v1 -- its agent prompt says so. Feeding (redacted) reasoning or the user's
messages to the reviewer is a follow-up, to be taken up when a review that
missed something for lack of them is named -- or when a goal-blind blocker
proves to be a false positive in practice (a `blocker` on a destructive
action the user explicitly asked for interrupts a turn that was doing what
it was told; the primary weighs the note as advice, and the per-review
outcome log records each steer, so such a case can be named).

### Epochs and turns

A lifecycle boundary — session remove/reset/reload, model/agent/workspace/
provider switch, native conversation reset, compaction/history rewrite,
fork/transfer destination creation, gateway shutdown/recycle — starts a new
observation epoch via `AdvisorObserver.begin_epoch()`. Pending records and
dedupe state (the emission guard resets with the epoch) never cross an epoch
boundary. Recording after a completed epoch raises until a new epoch begins.

A new TURN on the same session re-primes a sealed epoch via `begin_turn()`:
turn N+1 records land instead of raising, while a completed update the pump
has not consumed yet survives the re-prime. Attachment re-resolves the
per-session override every turn, so switching a live session to `off` drops
its observer and guard at the next turn boundary rather than being ignored.

Wiring: switch/reload resets notify through the dashboard's single reset
helper; slot close notifies at the synchronous tombstone; a successful
auto-compaction notifies through the session manager's compact callback (a
failed compact rewrote nothing and does not touch the epoch); gateway
shutdown disposes every observer via an `on_cleanup` hook. A fork or transfer
destination is a NEW session key with no observer, and the source is left
untouched by design, so both are fresh without a dedicated notification.

### Coverage boundary

Observation hooks live in the dashboard chat runner (`_run_chat`), so every
turn that runs through a dashboard chat slot is observed: interactive
chat, dashboard-bound goal loops and babysit wakes, and subagent-completion
injections. Turns that do not run through it -- task-executor steps,
channel `TurnDriver` turns, cron job turns, and subagent worker turns --
are NOT observed in v1; extending the hooks to those loops is a follow-up.

The observer attaches once per turn, after the provider session is acquired,
and only after the runner's conversation-binding recheck passes: a rebind that
lands while `get_or_create` is awaited refuses the turn (`memory_unavailable`,
retry) rather than attaching an observer keyed to another conversation while
this turn's events stream through it.

## Service contract

`AdvisorService` owns per-parent-session observers behind an enablement gate:

- Disabled (`enabled=False`, the default): `attach()` returns `None`, no
  observer exists, `observer_count()` is 0.
- Enabled mid-session: the observer starts empty at the current boundary;
  historical work is not backfilled or reviewed.
- Opted out mid-session: the next attach drops the live observer and guard.
- `detach()` disposes a session's observer. A terminal boundary also releases
  the parent's reviewer session on the pool; gateway disposal shuts the pool
  down so the shared subprocess dies with the gateway.

## Delivery contract

An advisory rides the same steer ledger as a user send through an additive
envelope parameter on the chat delivery seam: same pending registration, same
delivery-id reconciliation, same consumption evidence. The persisted row
carries the `advisor` role and provenance meta (`advisorSeverity`,
`advisorUpdateId`, `advisorState`); the injected text tells
the primary to weigh the evidence, never to obey it. The reviewer's text is
model output: besides outbound redaction, any reserved advisor delimiter it
carries (`[Advisor]`, `[Advisor context]`, `[End advisor context]`) is
neutralized where the steer text and the `[Advisor context]` frame are rendered (so a restored entry is covered too) -- together with the primary's own structural boundary markers via the platform's span-local neutralizer -- and a note cannot close or forge a frame
early and land its remainder as bare instructions. An advisory the turn
never consumed is PRESERVED at teardown -- a visible Advisor card plus context
staged for the next primary turn -- and never enters the user queue or runs
as a user-authored turn. The staged context is drained exactly once, at the
next turn's start, prepended to the outgoing message with the same
weigh-not-obey framing, and each entry passes the two outbound redactors again
at that provider egress (the persisted copy is disk state, not the redacted
text the reviewer produced). Both the staged context and the per-session override
are SESSION-scoped even though they live on slots: every live slot sharing
the effective session key (a channel-stem slot and a dashboard tab linked to
the same channel) is staged, read, drained, cleared, and overridden together
(staged in memory on every alias, since the list is persisted into the one
shared transcript from whichever alias saves next; only the acting slot is
dirtied -- on stage, drain and clear alike -- so a stale alias is never forced
to flush its own copy of the shared metadata over the active one's), so an
opt-out on one alias cannot be undone by a sibling's stale setting and advice
preserved on one alias reaches the session's next turn wherever it runs.
A slot object can also change conversation mid-turn (a cron/workflow swap of
`linked_session_key`): the observer key is pinned at attach and every later
checkpoint and pump drops on a mismatch, staged context is tagged with the session it
was staged for (re-bound on restore, after the slot's link is restored) and a
mismatch clears it unread, and the close boundary fires
only when the last slot fronting a session closes -- an idle alias closing
never drops a sibling's live observer. Slots also leave the registry without a
close (ephemeral OpenAI-compatible completions, fork and transfer failure
cleanups, history deletion), so whenever a new observer is created the service
retires every observer whose session no live slot fronts any more
(`AdvisorService.live_session_keys`, wired from the slot registry at startup),
through the same terminal boundary a close fires.
User sends without an envelope keep byte-identical
row and payload shapes. A hard kill discards pending advisories alongside
pending user steers.

## Usage attribution

A reviewer turn is real spend but not the parent's turn. Its usage row is
keyed by the reviewer session's stable synthetic key (`advisor:<parent session key>`, so spend stays traceable across restarts) and
tagged `surface="advisor"` -- the two existing token-record fields attribute
the spend, and the token-record schema is untouched. The parent link lives on
the advisory row itself (`advisorUpdateId`), not on the usage row.

## Configuration

Effective enablement composes the global default with a per-session override
(`inherit` / `on` / `off`): `on` and `off` win in both directions, and
an absent value is `inherit`, and anything else unrecognized on disk
collapses to `off` rather than to a default that could enable review. One
hydration (`chat_persistence.hydrate_advisor_meta`, also run by every first bind
of a cron tab -- the run-start pre-create, the delivery that creates the tab,
and the to-chat surfacing -- from transcript metadata prefetched off-loop) restores the override and
the staged context on every path that publishes a slot from disk -- history
rehydration, recent-session restore, the resume endpoint and the channel
session surface -- after the slot's session link is settled. The override endpoint mirrors the value onto every slot of the
session in memory and persists it ONCE, through the authorized slot (a failed
or rebound-around save rolls back only the writes this request itself made,
keyed by a per-slot mutation token rather than value equality -- an alias that
rebound and committed its own value meanwhile, even the same one, keeps it), the way
every other slot-metadata route does -- a forced save confirmed before the
200 (the periodic dirty flush skips message-less slots). Siblings are not
dirtied: the aliases share one transcript, and a sibling's full save would
rewrite the shared metadata from its own copy. The transaction is serialized
on the transcript's keyed lock (alias slots of one session share it, so a
losing request's rollback cannot undo the winner's acknowledged write) and
the save is pinned to the transcript key the request was authorized against,
so a rebind mid-save makes the write refuse; a rebind detected after the
write leaves the acknowledged value on the authorized transcript (200, applied
to that session's observer): every slot still bound to that transcript carries
the written value in memory (including an alias the channel reconciler created
during the save, which loaded the pre-write value), and only the rebound slot
gets its in-memory value back, since it now fronts another conversation.
On failure nothing reached disk and every member is restored in memory before
the coded error returns.
While the reviewer is unavailable (a non-kiro `agent.acp_backend`), `on`
is refused with the same reason the settings toggle gives (`409
advisor_unavailable`, shown inline by the control); `off` and `inherit`
always land.

An explicit `off` also throws away the advice still staged for the session's
next turn (every alias slot) and flips its preserved cards to
`dropped_by_opt_out` ("Dropped when you turned the Advisor off"), in place and
as a live `chat_message_update` patch, before the override save persists the
slot: injecting that advice under a control that reads "Disabled" would
contradict the control. `inherit` and `on` leave staged advice alone
(`delivery.drop_pending_advisor_context`). The drop follows the confirmed save
(a failed save answers 500 with override, advice and cards all rolled back) and
rides the dirty flush; a slot restored with `off` on disk applies the same rule
and hydrates no staged context.

History rewrites -- regenerate, switch variant, edit-resend, rewind -- are an
advisor boundary too (`service.notify_history_rewrite`, called at each route's
commit): a final review still running about the replaced turns is discarded like
a hard kill's, and advice staged for them is cleared rather than injected into
the replacement timeline. In edit-resend and rewind the boundary runs before the
worker rewrite persists the slot's metadata, so the disk copy carries no staged
advice a restart could rehydrate. A rewrite whose save fails, is refused, or is
cancelled before landing leaves the original timeline, and
`service.restore_staged_advice` puts its staged advice back -- under the session
key the slot showed when the rewrite started; a slot rebound meanwhile keeps
nothing, since the advice was about the other session. The opt-out drop re-labels only cards whose
advice was still staged (delivered history keeps its label) and only while the
slot still fronts the transcript the request authorized.

Revocation stops a reviewer turn that has already been transmitted: the pool runs
each prompt as its own task, `release_session` (the path an opt-out or reset
takes) cancels it before disposing the session, and a hard kill cancels it
through `cancel_active` while keeping the session, as does every non-close
lifecycle boundary (reset, compaction, clear); `review` returns `None` to its
caller for a turn cancelled this way. A steer whose authorization is revoked while its RPC
is in flight is retired on return: the steered row is removed from the
transcript (the slot may front another conversation by then), live clients get
a blanked `discarded` patch flagged `advisorRemoved` (live clients drop the row rather than render a blank card), the pending registration is dropped, and the
dispatcher learns `revoked`, not `steered`. A hard kill seals observation until
the next turn attaches: a late record or completion from the runner being torn
down is dropped and produces no review. Permission-denial reasons are redacted
and bounded before they reach a log. Every in-flight prompt of the parent is
tracked (a checkpoint pump and a terminal pump can overlap), so all of them
stop. The turn teardown does not stage an unconsumed steered advisory for a
session whose override is `off` (its row is relabelled `dropped_by_opt_out`),
and a hard kill drops staged advice along with the in-flight work (cards
`discarded`).

The reviewer process is recycled by the runtime's own staleness probe
(age / RSS) between turns -- never while a prompt is in flight. A global
disable releases every detached inherited session through the pool, so its
in-flight review is cancelled and the runtime can reap once the explicit-on
sessions are gone. The opt-out relabel spends one staged occurrence per card,
newest first, so an older delivered card with identical text keeps its label.
The spec install re-checks ownership of the reserved agent name under
`agents_spec_lock` immediately before its atomic write, so a user file
published during the probe window is refused, not replaced.

## Review pump

Observation flows to review asynchronously, DURING the turn as well as at
its end. The chat runner feeds the observer at host-owned checkpoints (turn
attach, redacted tool results, finalized segments, one idempotent final
update at the terminal event) and schedules a fire-and-forget pump at each
checkpoint the primary turn never awaits — so in-progress advice can arrive
while a mistake is still cheap to fix. Each update carries the epoch's
CUMULATIVE evidence (bounded to `EPOCH_MAX_RECORDS`, newest wins): a live
run proved slice-at-a-time reviews myopic — twenty reviews each saw one
innocuous fragment and missed what one whole-turn review caught. Drains are
gated on genuinely new records, and in-progress reviews are throttled per
session (`review_min_interval_secs`); the final update always reviews. A
review that a reset, compaction, or opt-out raced is discarded before
dispatch (observer identity and epoch are re-checked), and the same live
check runs again before every note of one review -- a blocker steer awaits
the running turn, so a revocation can land between two notes, and the
remaining notes are then dropped (`revoked`) rather than persisted, staged,
or steered -- so stale advice never
crosses into a replacement conversation. The pump drains one update, renders it as the reviewer
prompt, runs one bounded review on the shared pool, decodes the JSON
envelope from the reviewer's final text with the platform's `parse_llm_json`
(bare, fenced, or prose-embedded; validation stays strict), and dispatches through the per-session emission
guard. Every failure leg — no observer, empty drain, unbound pool, reviewer
error, malformed output — ends the pump quietly; a lost review logs its
cause at WARNING, and every completed review logs one INFO line with its note count and dispatch
outcomes, so a silent reviewer and a broken dispatcher are distinguishable
in production. Degradation is not yet surfaced in the dashboard UI; #10135
tracks a `degraded` state on the per-session control.
Enablement is decided by the ATTACHED OBSERVER, never re-checked against
the global flag: attach composed the global default with the per-session
override, so a session opted `on` under a global-off default reviews.
The reviewer's own text passes outbound redaction (credentials,
exfiltration URLs) before it is injected or persisted — reviewer output is
model output. A `blocker` steered into a turn that never consumed it flips
its existing card to preserved rather than duplicating it.

The reviewer runs as the packaged toolless `kirocrew-advisor` agent
(`advisor/agents/kirocrew-advisor.json`; `tools` is empty,
`includeMcpJson` is false, and the spec declares no MCP servers or hooks).
One shared runtime process serves isolated reviewer sessions for every enabled
parent session. The observed project path is prompt context for relative paths
already present in primary tool results; it does not grant project access.

The managed spec is a kiro-cli agent definition, so the reviewer runtime is
kiro-cli only in v1. `AdvisorService.reviewer_available` is true exactly when
`agent.acp_backend` selects kiro, on every platform that can run that backend.
The config PATCH surface refuses global enablement with a visible reason before
the live configuration snapshot is available or while another backend is
selected. A per-session `on` returns `409 advisor_unavailable` under another
backend; `off` and `inherit` always land. `configure_from_config` disables the
advisor, detaches every observer, and discards in-flight reviews when the
backend is not kiro. A live backend PATCH re-applies the section. Serving the
reviewer on the Claude/Codex backends is a follow-up.

The spec is a managed artifact. Before every reviewer-process spawn, the
packaged JSON is parsed and atomically installed under
`<kiro home>/kirocrew-advisor/agents`; no disk grant is merged into it, so
`tools` stays empty. A regular file at the reserved name that is not the
managed spec (its description does not open with the managed sentence) is
somebody's configuration: installation refuses with a name-collision error
and leaves it intact. The reviewer opens from the crew-owned
`<config>/advisor` directory, and installation refuses while a `.kiro` tree
there could shadow the managed agent.

## Security posture

The reviewer sees only the rendered `ObservationUpdate`: update identity and
phase, cumulative finalized assistant segments, and the primary agent's
completed tool results. The chat runner supplies the same redacted segment and
tool-result text used by the transcript. Each record is bounded to
`OBSERVATION_PAYLOAD_MAX_CHARS`; clipped segments carry a truncation marker,
clipped tool results carry `truncated=True`, and `EPOCH_MAX_RECORDS` bounds the
combined epoch history. The observed project path is JSON-encoded above the
prompt's data boundary only to interpret relative paths already present in
those results.

The agent has no callable tools. It cannot open a project file, run a command,
or call MCP to gather evidence beyond the observation. This is the primary
containment boundary: a reviewer that opens untrusted project content with its
own tools needs an OS read boundary that cannot be complete on every supported
platform, while a toolless reviewer receives the primary's relevant evidence
through the host-owned checkpoint channel and has no project-content access of
its own.

Any permission request from the toolless agent is anomalous. The composition's
deny-all permission gate rejects it and `agent_sdk.oneshot` records the denial
in the Security Event Log. An unexpected tool-call event is also recorded; if
that critical audit write fails, the review is abandoned and its reply does
not contribute advice. `prompt_for_reply` requires a `proceed` authorization,
re-checks it after the reviewer session opens and at every permission decision,
and cancels without a reply when the session is revoked.

The one-shot runtime requests the STRICT sandbox tier where the host can apply
it, as a best-effort layer rather than an availability precondition. It uses a
private `KIRO_HOME` at `<kiro home>/kirocrew-advisor`, a dedicated
subdirectory of the Kiro home: the managed spec lives under `agents/` and
reviewer sessions under `sessions/cli`, beside and apart from the operator's
own agents and sessions. That location is writable by a kiro-cli child under
every sandbox tier (the crew data home's leaves are sealed read-only or
hidden), and the spawn requests no writable or visible overrides. The
`agents/` leaf is the exception, because the spec there is what makes the
reviewer toolless: it is sealed exactly like the operator's `~/.kiro/agents`
tree (`config.paths.advisor_agents_dir`), mounted read-only inside every
Linux namespace and denied for writes by the macOS Seatbelt profile
(`sandbox._resolved_kiro_agents_targets`), pre-created so the seal has a real
directory to hold before the advisor is ever enabled (both backends create
the private home; the Linux launcher also creates the `agents` leaf it must
bind, while a Seatbelt deny holds for an absent name), refused as an
overlapping workspace on the platforms that
delegate to kiro-cli's own sandbox, and on the agents' file-edit
write-protected list (`security.paths._ADVISOR_AGENTS_DIR`, re-anchored under
a `KIRO_HOME` override). The components the reviewer resolves its spec
through are held as real directories, not names a sandboxed process can
retarget: a link at the private home or at its `agents` leaf refuses the
spawn (no-follow, before and after creation), the Seatbelt profile denies
writes and links to the home's own directory entry so it cannot be created,
renamed or unlinked from inside a sandbox while `sessions/` beneath stays
writable (every ancestor of the home, `<kiro home>` included, carries the same
literal deny, pinned from the advisor home itself so a relocated crew data
home or a `KIRO_HOME` override does not leave a parent unguarded; on Linux
the home is a top-level leaf of the Kiro home, the same posture as the
operator's agents tree and the crew data home), and the gateway installer
refuses to write the
spec through a linked component on every platform
(`atomic_write.refuse_linked_parent`).
Only the gateway, unsandboxed, writes the spec. The one path
override it does request is conditional: where the strict tier applies a path
mask on this host (`sandbox.credential_mask_applies("strict")`, probed off-loop
before the spawn), the crew-home leaves that stay read-write for a primary's
in-sandbox MCP servers (the SEL trust root and key, the security-event log,
the dashboard secret; `sandbox.mcp_only_crew_leaf_targets`) are hidden from
the reviewer child, which runs no MCP server; where the host delegates the
child to kiro-cli's own sandbox instead (Windows), a path override would fail
the spawn closed, so none is requested there and the toolless spec is what
bounds the child. Together these keep the reviewer spawnable everywhere the
kiro backend is.
`AcpRuntime(credential_free_env=True)` removes `KIRO_API_KEY` from the child
environment, leaving kiro-cli's own login-store authentication.
`expect_mcp_reports=False` skips MCP startup reporting because the reviewer
runs no MCP server. Platform sandbox capability does not participate in
`reviewer_available`.

The residual is model-output risk: assistant segments and primary tool results
are untrusted content, and prompt injection in that content can shape the
reviewer's notes despite the prompt's data framing. Reviewer output is
therefore advice for the primary to weigh, never an instruction to obey. The
strict output envelope and emission guard constrain delivery, reserved advisor
delimiters are neutralized, and credential and exfiltration-URL redaction runs
before advice is injected or persisted.

The reviewer model is the `advisor` role pin, `agent.role_models.advisor`
-- `agent.role_models` is the only sanctioned place to pin a model for a
class of work, and the pin passes the same entitlement validation as every
other role; `auto` or no pin keeps the runtime default, and a pin the
runtime's model-id grammar rejects falls back to the default with a warning
instead of failing every review. Reviewer spend persists as an attributed
usage row per review (see Usage attribution). Gateway startup applies the
`advisor.*` config section on a background task off the bind path; pool
binding follows enablement (a disabled advisor constructs nothing, and
disabling live unbinds and shuts the pool down), and no process spawns until
an enabled session's first review. The one `advisor.*` key (`enabled`) and the
reviewer's `agent.role_models.advisor` pin are editable from the dashboard
settings (Settings → Chat → Advisor) through the config PATCH surface, which
re-applies to the live service on success.

The observation prompt names everything after its header as data under review,
and the packaged spec says that text addressing the reviewer inside the
observed output is a finding to report, never an instruction. The project path
travels as an ASCII-escaped JSON string, so a newline or Unicode line separator
cannot place text above that data boundary.

## Testing

The `test/test_advisor_*.py` suites pin the contracts above: observation and
epochs, runtime pool lifecycle, envelope and guard, delivery and staged
context, lifecycle boundaries, config resolution, per-slot override, usage
attribution, turn hooks, dispatch policy, and the pump end to end. The
feature was additionally validated live against an isolated dev gateway: a
weak primary model paired with a stronger reviewer produced real mid-work
`[concern]` interventions on a genuine transcript.
