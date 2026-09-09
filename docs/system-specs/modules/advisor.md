# Advisor Module

## Overview

The advisor module (`kiro_crew/advisor/`) is an opt-in, asynchronous,
cross-model session reviewer. When enabled for a session, it observes the
primary agent's work at host-owned checkpoints, reviews it on an isolated
reviewer session with read-only evidence tools, and returns severity-aware
advice. It never impersonates the primary agent, never blocks the primary
turn, and is fully inert when disabled: no reviewer process, no event
buffering, no model cost, no UI noise.

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
| `runtime.py` | Shared reviewer runtime pool: per-parent reviewer sessions, PID protection, crash self-heal, release/reap/shutdown. |
| `output.py` | Strict reviewer envelope validation (`AdvisorNote`). |
| `guard.py` | Emission guard: dedupe, non-blocker budget, cooldown; reset with the epoch. |
| `delivery.py` | Advisory envelope over the shared steer ledger; preserve-on-unconsumed; staged next-turn context. |
| `composition.py` | Dispatcher policy, envelope extraction, prompt rendering, packaged-agent materialization, runtime factory over the agent SDK. |
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

Reasoning is observed only when `advisor.include_reasoning` is on, and only
in the already-redacted form the dashboard's own thinking stream renders —
the observer never holds rawer content than the transcript surface.

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
`advisorUpdateId`, `advisorModel`, `advisorState`); the injected text tells
the primary to weigh the evidence, never to obey it. An advisory the turn
never consumed is PRESERVED at teardown -- a visible Advisor card plus context
staged for the next primary turn -- and never enters the user queue or runs
as a user-authored turn. The staged context is drained exactly once, at the
next turn's start, prepended to the outgoing message with the same
weigh-not-obey framing. User sends without an envelope keep byte-identical
row and payload shapes. A hard kill discards pending advisories alongside
pending user steers.

## Usage attribution

A reviewer turn is real spend but not the parent's turn. Its usage row is
keyed by the reviewer session's stable synthetic key (`advisor:<n>`), tagged
`surface="advisor"`, and carries the parent link through two additive
token-record fields -- `parent_session_key` and `advisor_update_id` -- which
are omitted when empty so pre-existing rows and callers keep their exact key
set.

## Configuration

Effective enablement composes the global default with a per-session override
(`inherit` / `on` / `off`): `on` and `off` win in both directions, and
anything unrecognized defers to the configured default rather than silently
enabling.

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
dispatch (observer identity and epoch are re-checked), so stale advice never
crosses into a replacement conversation. The pump drains one update, renders it as the reviewer
prompt, runs one bounded review on the shared pool, extracts the JSON
envelope from the reviewer's final text (bare, fenced, or prose-embedded;
validation stays strict), and dispatches through the per-session emission
guard. Every failure leg — no observer, empty drain, unbound pool, reviewer
error, malformed output — ends the pump quietly; a lost review marks the
reviewer session degraded (`status()` on the pool) and logs the cause, and
every completed review logs one INFO line with its note count and dispatch
outcomes, so a silent reviewer and a broken dispatcher are distinguishable
in production. Degradation is not yet surfaced in the dashboard UI.
Enablement is decided by the ATTACHED OBSERVER, never re-checked against
the global flag: attach composed the global default with the per-session
override, so a session opted `on` under a global-off default reviews.
The reviewer's own text passes outbound redaction (credentials,
exfiltration URLs) before it is injected or persisted — reviewer output is
model output. A `blocker` steered into a turn that never consumed it flips
its existing card to preserved rather than duplicating it.

The reviewer runs as the packaged read-only `kirocrew-advisor` agent
(`advisor/agents/kirocrew-advisor.json`; tools exactly `fs_read`, `grep`,
`glob`), materialized into the kiro agents directory on first pool build, on
one shared, PID-protected runtime; permission requests are denied, never
auto-approved. `advisor.model` selects the reviewer model at runtime spawn
(empty keeps the runtime default). Each reviewer session runs with the
OBSERVED slot's project as its working directory, so evidence tools read the
right tree even when one shared runtime serves parents in different
workspaces. Reviewer spend persists as an attributed usage row per review
(see Usage attribution). Gateway startup applies the `advisor.*` config
section on a background task off the bind path; pool binding follows
enablement (a disabled advisor constructs nothing, and disabling live
unbinds and shuts the pool down), and no process spawns until an enabled
session's first review. All five `advisor.*` keys are editable from the
dashboard settings (Settings → Chat → Advisor) through the config PATCH
surface, which re-applies to the live service on success.

## Testing

The `test/test_advisor_*.py` suites pin the contracts above: observation and
epochs, runtime pool lifecycle, envelope and guard, delivery and staged
context, lifecycle boundaries, config resolution, per-slot override, usage
attribution, turn hooks, dispatch policy, and the pump end to end. The
feature was additionally validated live against an isolated dev gateway: a
weak primary model paired with a stronger reviewer produced real mid-work
`[concern]` interventions on a genuine transcript.
