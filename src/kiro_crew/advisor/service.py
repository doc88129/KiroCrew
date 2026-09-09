"""Advisor service: parent-session registry and effective enablement.

The service is the single owner of advisor state for live sessions. When the
effective setting is off it is inert: no observer is created, nothing is
buffered, and no reviewer runtime exists (grounding: architecture constraint
that disabled means zero cost).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.advisor.observation import AdvisorObserver, ObservationUpdate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.advisor.guard import EmissionGuard

logger = logging.getLogger(__name__)

#: Boundary reasons. Epoch-scoped reasons re-prime the observer in place;
#: terminal reasons dispose it.
BOUNDARY_RESET = "reset"
BOUNDARY_COMPACTION = "compaction"
BOUNDARY_REWRITE = "rewrite"
BOUNDARY_CLOSE = "close"
BOUNDARY_REMOVE = "remove"
BOUNDARY_TRANSFER = "transfer"

_EPOCH_REASONS = frozenset({BOUNDARY_RESET, BOUNDARY_COMPACTION, BOUNDARY_REWRITE})
_TERMINAL_REASONS = frozenset({BOUNDARY_CLOSE, BOUNDARY_REMOVE, BOUNDARY_TRANSFER})

#: Per-session override values. ``inherit`` defers to the global setting.
OVERRIDE_INHERIT = "inherit"
OVERRIDE_ON = "on"
OVERRIDE_OFF = "off"


def resolve_effective_enabled(global_enabled: bool, override: object) -> bool:
    """The session's effective advisor enablement.

    ``on``/``off`` win over the global default in both directions; anything
    else -- ``inherit``, an empty value, or a value written by a future
    version this build does not know -- defers to the configured default
    rather than silently enabling.
    """
    if override == OVERRIDE_ON:
        return True
    if override == OVERRIDE_OFF:
        return False
    return bool(global_enabled)


class AdvisorService:
    """Registry of per-parent-session advisor observers.

    v1 scope: enablement gate and observer lifecycle. Reviewer runtime
    attachment, guard, and delivery policy compose on top of this registry.
    """

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled
        self._observers: dict[str, AdvisorObserver] = {}
        self.reviewer_model: str = ""
        self.non_blocker_budget: int = 4
        self.cooldown_secs: float = 120.0
        self.include_reasoning: bool = False
        #: Minimum seconds between IN-PROGRESS reviews per session; the final
        #: update always reviews. Guards reviewer spend against checkpoint
        #: storms (a live run produced 20 reviews on one turn without it).
        self.review_min_interval_secs: float = 20.0
        self._last_review_at: dict[str, float] = {}
        #: Bumped on every reset/compaction/rewrite/opt-out/terminal boundary
        #: for a session; the pump uses it to tell a raced boundary apart from
        #: an ordinary next-turn re-prime (which never bumps it).
        self._boundary_gen: dict[str, int] = {}
        #: Each live observer's override source (inherit|on) -- a global
        #: disable detaches inherited observers immediately but keeps the
        #: explicitly opted-in ones, so it must know which is which.
        self._override_source: dict[str, str] = {}
        self._pool: object | None = None
        #: The reviewer model the live pool was built for; a config change to
        #: a different model must replace the pool, not keep the stale one.
        self._pool_model: str = ""
        self._guards: dict[str, "EmissionGuard"] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def attach(
        self, parent_session_key: str, override: str = OVERRIDE_INHERIT
    ) -> AdvisorObserver | None:
        """Attach an observer for a parent session at the current boundary.

        ``override`` is the slot's persisted ``inherit|on|off`` selection,
        composed with the global default by ``resolve_effective_enabled``.
        Returns None when the effective setting is off. Enabling mid-session
        starts from the current boundary: the fresh observer holds no
        historical records.
        """
        if not resolve_effective_enabled(self._enabled, override):
            # Opt-out is honored at every turn, not only at first attach:
            # drop the live observer and its guard so observation stops now.
            self._observers.pop(parent_session_key, None)
            self._guards.pop(parent_session_key, None)
            self._override_source.pop(parent_session_key, None)
            self._boundary_gen[parent_session_key] = (
                self._boundary_gen.get(parent_session_key, 0) + 1
            )
            return None
        self._override_source[parent_session_key] = override
        observer = self._observers.get(parent_session_key)
        if observer is None:
            observer = AdvisorObserver(parent_session_key=parent_session_key, turn_id="")
            self._observers[parent_session_key] = observer
            # A session opted in under a globally-off default still needs a
            # reviewer pool; configure_from_config deliberately constructs
            # nothing while disabled, so bind lazily at first effective use.
            if self._pool is None:
                _ensure_pool_bound(self)
        else:
            # A sealed epoch means the previous turn finished: re-prime so
            # this turn's records land instead of raising, preserving any
            # completed update the pump has not consumed yet. A new turn also
            # resets dedupe, so a blocker repeated on turn 2 is not suppressed
            # by turn 1's guard state.
            if observer.epoch_completed:
                self._guards.pop(parent_session_key, None)
            observer.begin_turn()
        return observer

    def detach(self, parent_session_key: str) -> None:
        """Dispose the observer for a parent session, if any."""
        self._observers.pop(parent_session_key, None)

    def observer_count(self) -> int:
        return len(self._observers)

    def set_reviewer_pool(self, pool: object) -> None:
        """Bind the reviewer pool (real or fake) used by the pump."""
        self._pool = pool

    async def pump_async(self, state: object, slot: object) -> None:
        """Drain the slot's pending observation and run one bounded review.

        The full asynchronous path: drain -> render -> pool review ->
        envelope extraction -> guarded, severity-routed dispatch. Total: a
        disabled service, an absent observer, an empty drain, a missing
        pool, and a failed review all end the pump quietly -- the primary
        turn never waits on, or breaks because of, the advisor.
        """
        # Gate on the ATTACHED observer, never the global flag: attach_for_turn
        # already composed the global default with the per-session override, so
        # an observer's existence IS the enablement decision -- a session opted
        # "on" under a global-off default reviews, and an opted-out session
        # (observer dropped at attach) does not.
        observer = self._observers.get(_slot_session_key(slot))
        pool = getattr(self, "_pool", None)
        if observer is None or pool is None:
            return
        import time as _time

        session_key_early = _slot_session_key(slot)
        # Acquire the reviewer session BEFORE the destructive drain: a runtime
        # spawn failure here must neither crash the pump task nor consume the
        # update -- the observer keeps it and the next pump retries.
        try:
            await pool.acquire_session(session_key_early)
        except Exception:
            logger.warning(
                "advisor reviewer session acquire failed for %s; update retained",
                session_key_early,
                exc_info=True,
            )
            return
        update: "ObservationUpdate | None" = observer.take_completed()
        if update is None:
            last = self._last_review_at.get(session_key_early, 0.0)
            if _time.monotonic() - last < self.review_min_interval_secs:
                return  # throttled: the next checkpoint or terminal catches up
            update = observer.drain_update()
        if update is None:
            return
        from kiro_crew.advisor.composition import (
            AdvisorDispatcher,
            extract_envelope,
            render_update_prompt,
        )
        from kiro_crew.advisor.guard import EmissionGuard

        session_key = _slot_session_key(slot)
        advisor_update_id = f"{session_key}:{update.epoch}:{update.seq}"
        payload = {
            "prompt": render_update_prompt(update),
            "seq": update.seq,
            "in_progress": update.in_progress,
            # The reviewer session's evidence tools read the OBSERVED slot's
            # tree; empty when the slot has no project (gateway cwd fallback).
            "work_dir": str(getattr(slot, "project", "") or ""),
            # Precomputed so the reviewer's usage row and the advisory row
            # share one identity for cross-referencing spend to advice.
            "advisor_update_id": advisor_update_id,
        }
        review_gen = self._boundary_gen.get(session_key, 0)
        review_epoch = update.epoch
        self._last_review_at[session_key] = __import__("time").monotonic()
        raw = await pool.review(session_key, payload)
        if raw is None:
            return
        # A reset/compaction/opt-out that RACED the review must discard its
        # stale result -- but a same-conversation next turn must NOT, or the
        # final review the pump already took gets dropped. So gate on the
        # boundary GENERATION (bumped only by a true boundary, never by a
        # next-turn re-prime) and on the observer still being the live one.
        current = self._observers.get(session_key)
        if current is not observer or self._boundary_gen.get(session_key, 0) != review_gen:
            logger.debug("advisor review discarded: session boundary during review")
            return
        # The SLOT's identity can move under the review too: a cron/workflow
        # rebind swaps `linked_session_key`, so the slot now fronts a different
        # conversation while the old key's observer and generation are both
        # unchanged. Recompute the effective key -- advice reviewed for one
        # conversation must never persist or stage into its replacement.
        if _slot_session_key(slot) != session_key:
            logger.info(
                "advisor review discarded: slot rebound from %s to %s during review",
                session_key,
                _slot_session_key(slot),
            )
            return
        # Same conversation, but the NEXT turn began while the review ran: the
        # advice describes a finished turn, so steering it into the successor
        # would interrupt work it never observed. Preserve instead (card +
        # staged context) -- the advice still reaches the next prompt.
        steer_allowed = observer._epoch == review_epoch
        guard = self._guards.setdefault(
            session_key,
            EmissionGuard(
                non_blocker_budget=self.non_blocker_budget,
                cooldown_secs=self.cooldown_secs,
            ),
        )
        dispatcher = AdvisorDispatcher(guard=guard, reviewer_model=self.reviewer_model)
        try:
            envelope = extract_envelope(raw)
        except Exception:
            envelope = raw  # dispatcher records the degradation uniformly
        outcomes = await dispatcher.dispatch(
            state,
            slot,
            envelope,
            advisor_update_id=advisor_update_id,
            steer_allowed=steer_allowed,
        )
        # One INFO line per review: the only production signal that says
        # whether advice flowed, was suppressed, or degraded. A silent
        # reviewer and a broken dispatcher look identical without it.
        logger.info(
            "advisor review %s: %s",
            advisor_update_id,
            outcomes if outcomes else "no notes (or all suppressed)",
        )

    def notify_boundary(self, parent_session_key: str, reason: str) -> None:
        """Join a parent lifecycle boundary. Total: never raises.

        Epoch-scoped reasons (reset, compaction, rewrite) start a new
        observation epoch so pending records and dedupe state cannot cross a
        rewritten conversation. Terminal reasons (close, remove, transfer)
        dispose the observer. Gated on OBSERVER PRESENCE, not the global
        flag: a session opted in while the global default is off still has
        live state that must not survive its boundaries. Unknown sessions
        are no-ops -- a lifecycle path must never fail because of the
        advisor.
        """
        observer = self._observers.get(parent_session_key)
        if observer is None:
            return
        # Every real boundary bumps the generation so a review racing it is
        # discarded (distinct from an ordinary next-turn re-prime).
        self._boundary_gen[parent_session_key] = self._boundary_gen.get(parent_session_key, 0) + 1
        if reason in _TERMINAL_REASONS:
            self._observers.pop(parent_session_key, None)
            self._guards.pop(parent_session_key, None)
            self._schedule_pool_release(parent_session_key)
            return
        # Epoch-scoped by default: an unrecognized future reason re-primes
        # rather than silently keeping stale state. The guard resets with
        # the epoch -- dedupe and cooldown must not suppress a repeated
        # blocker across a reset or compaction.
        observer.begin_epoch()
        self._guards.pop(parent_session_key, None)

    def dispose_all(self) -> None:
        """Drop every observer and guard (gateway shutdown/recycle).

        Also schedules the reviewer pool's shutdown so the shared subprocess
        dies with the gateway instead of orphaning. Total and non-blocking.
        """
        self._observers.clear()
        self._guards.clear()
        pool = self._pool
        if pool is not None:
            self._pool = None
            self._spawn_bg(getattr(pool, "shutdown", None))

    def _schedule_pool_release(self, parent_session_key: str) -> None:
        """Release the parent's reviewer session without blocking the caller."""
        pool = self._pool
        if pool is None:
            return
        release = getattr(pool, "release_session", None)
        if release is None:
            return
        self._spawn_bg(release, parent_session_key)

    @staticmethod
    def _spawn_bg(fn: object, *args: object) -> None:
        """Run an async pool operation as a fire-and-forget task. Total."""
        if fn is None:
            return
        import asyncio

        try:
            asyncio.get_running_loop().create_task(fn(*args))  # type: ignore[operator]
        except RuntimeError:  # no running loop (sync context/tests)
            logger.debug("pool op skipped: no running event loop")
        except Exception:
            logger.debug("pool op scheduling failed", exc_info=True)


_service: AdvisorService | None = None


def get_advisor_service() -> AdvisorService:
    """The process-wide advisor service (disabled until configured on)."""
    global _service
    if _service is None:
        _service = AdvisorService(enabled=False)
    return _service


def configure_from_config(cfg: object) -> None:
    """Apply the ``advisor.*`` config section to the process-wide service.

    Total: junk or missing sections leave the service in its current state
    (disabled by default). Called at gateway startup and config reload.
    """
    advisor = getattr(cfg, "advisor", None)
    if advisor is None:
        return
    service = get_advisor_service()
    service._enabled = bool(getattr(advisor, "enabled", False))
    service.reviewer_model = str(getattr(advisor, "model", "") or "")
    # `or` would coerce an operator-set 0 (a valid "no budget" / "no
    # cooldown" choice the write gate accepts) back to the default.
    _budget = getattr(advisor, "non_blocker_budget", None)
    service.non_blocker_budget = 4 if _budget is None else int(_budget)
    _cooldown = getattr(advisor, "cooldown_secs", None)
    service.cooldown_secs = 120.0 if _cooldown is None else float(_cooldown)
    service.include_reasoning = bool(getattr(advisor, "include_reasoning", False))
    # Pool binding follows enablement: a disabled advisor constructs NOTHING
    # -- a default-off feature must not construct eagerly at startup --
    # and disabling live unbinds + schedules the pool's shutdown so the
    # settings toggle governs the whole lifecycle. The pool itself spawns no
    # process until an enabled session's first review.
    if service._enabled or service._pool is not None:
        # (Re)bind when there is no pool, or when the reviewer model changed
        # under us — a stale pool would run the old model while rows are
        # labeled with the new one. A pool bound for an opted-in session
        # under a globally-off default follows model changes the same way.
        _ensure_pool_bound(service)
    if not service._enabled:
        # Disabling must stop INHERITED observation immediately -- the next
        # checkpoint fires before the next attach, so waiting for attach's
        # opt-out pass would keep feeding the reviewer after the operator
        # said stop. Explicit per-session `on` survives: that session chose
        # observation independently of the global default.
        for key in [k for k, src in list(service._override_source.items()) if src != OVERRIDE_ON]:
            service._observers.pop(key, None)
            service._guards.pop(key, None)
            service._override_source.pop(key, None)
            service._boundary_gen[key] = service._boundary_gen.get(key, 0) + 1
    if not service._enabled and not service._observers and service._pool is not None:
        pool = service._pool
        service._pool = None
        service._pool_model = ""
        service._spawn_bg(getattr(pool, "shutdown", None))


def _ensure_pool_bound(service: "AdvisorService") -> None:
    """Bind (or rebind) the reviewer pool for the configured model.

    Idempotent for an unchanged model; a changed model replaces the pool and
    schedules the old one's shutdown so review processes never outlive their
    configuration. Shared by config (re)application and the lazy bind at a
    session's first effective use.
    """
    if service._pool is not None and service._pool_model == service.reviewer_model:
        return
    old_pool = service._pool
    try:
        from kiro_crew.advisor import composition

        service.set_reviewer_pool(composition.build_reviewer_runtime(service.reviewer_model))
        service._pool_model = service.reviewer_model
        if old_pool is not None:
            service._spawn_bg(getattr(old_pool, "shutdown", None))
    except Exception:
        logger.warning("advisor pool bind failed", exc_info=True)


def _slot_session_key(slot: object) -> str:
    """The session key *slot*'s turns run on — the advisor's registry key.

    MUST agree with ``chat_utils.effective_session_key``: reset and compaction
    boundaries are fired with that key, so an observer registered under any
    other spelling would never receive them. A channel-linked slot's turns run
    on the channel's own session (``linked_session_key``); everything else
    derives from the slot key. Mirrored here (with a late import) so the
    advisor package works on the same objects without a hard dashboard
    dependency; the parity is pinned by a test against the real helper.
    """
    linked = getattr(slot, "linked_session_key", "")
    if linked:
        return str(linked)
    key = getattr(slot, "key", "")
    if not key:
        return ""
    return key if key.startswith("dashboard:") else f"dashboard:{key}"


def attach_for_turn(slot: object) -> AdvisorObserver | None:
    """Attach (or fetch) the slot's observer for a starting turn.

    Composes the global default with the slot's persisted override. Cheap and
    inert when the effective setting is off: one dict lookup, no buffering.
    """
    service = get_advisor_service()
    override = getattr(slot, "advisor_override", OVERRIDE_INHERIT)
    session_key = _slot_session_key(slot)
    if not session_key:
        return None
    observer = service.attach(session_key, override=override)
    if observer is not None:
        # Parent-turn identity for every record this turn produces; refreshed
        # each attach so a multi-turn observer never reports a stale turn.
        observer._turn_id = f"turn-{getattr(slot, '_turn_generation', 0)}"
    return observer


def _observer_for(slot: object) -> AdvisorObserver | None:
    service = get_advisor_service()
    return service._observers.get(_slot_session_key(slot))


def observe_tool_result(slot: object, tool_name: str, payload: str) -> None:
    """Record a completed tool result for the slot's observer, if any.

    Total: called from the chat runner's hot event loop, so a broken observer
    degrades silently rather than breaking the turn.
    """
    observer = _observer_for(slot)
    if observer is None:
        return
    try:
        observer.record_tool_result(tool_name, payload)
    except Exception:  # advisor must never break the primary turn
        logger.debug("advisor observe_tool_result failed", exc_info=True)


def observe_segment(slot: object, text: str) -> None:
    """Record a finalized assistant segment for the slot's observer, if any."""
    observer = _observer_for(slot)
    if observer is None:
        return
    try:
        observer.record_segment(text)
    except Exception:
        logger.debug("advisor observe_segment failed", exc_info=True)


def observe_reasoning(slot: object, text: str) -> None:
    """Record already-redacted reasoning, only when the config opts in.

    The gate lives here (not at the call site) so the runner stays a dumb
    forwarder and the config read has exactly one home. Total.
    """
    service = get_advisor_service()
    if not service.include_reasoning:
        return
    observer = _observer_for(slot)
    if observer is None:
        return
    try:
        observer.record_reasoning(text)
    except Exception:
        logger.debug("advisor observe_reasoning failed", exc_info=True)


def complete_turn(slot: object, stop_reason: str | None = None, synthetic: bool = False):
    """Emit the turn's final observation update, if an observer is attached."""
    observer = _observer_for(slot)
    if observer is None:
        return None
    try:
        return observer.complete(stop_reason=stop_reason, synthetic=synthetic)
    except Exception:
        logger.debug("advisor complete_turn failed", exc_info=True)
        return None


def schedule_pump(state: object, slot: object) -> None:
    """Fire-and-forget one review pump for the slot, when warranted.

    Called from the chat runner's event loop. Cheap pre-checks (enabled,
    observer present, pool bound) run synchronously so a disabled advisor
    costs two dict lookups and no task; the pump itself runs as a background
    task the primary turn never awaits.
    """
    import asyncio

    service = get_advisor_service()
    if getattr(service, "_pool", None) is None:
        return
    if _slot_session_key(slot) not in service._observers:
        return
    try:
        task = asyncio.get_running_loop().create_task(service.pump_async(state, slot))
    except RuntimeError:  # no running loop (sync test context)
        return
    tasks = getattr(state, "_background_tasks", None)
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
