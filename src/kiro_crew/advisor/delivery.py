"""Advisory delivery over the existing chat steer ledger.

An advisory rides the SAME ledger as a user steer -- pending registration,
delivery ids, consumption evidence, teardown reconciliation -- through an
additive envelope parameter on ``steer_into_running_turn``. What differs is
identity and fate: the persisted row carries the ``advisor`` role and
provenance meta, and an advisory the turn never consumed is PRESERVED (a
visible Advisor card plus context staged for the next primary turn) rather
than requeued as user speech. An advisory must never enter the user queue or
execute as a user-authored turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.advisor.output import AdvisorNote

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: Outcomes of :func:`deliver_advisory`.
ADVISORY_STEERED = "steered"
ADVISORY_PRESERVED = "preserved"
ADVISORY_DISCARDED = "discarded"

#: Row/meta states an advisor row can carry in ``meta["advisorState"]``.
ADVISOR_STATE_STEERED = "steered"
ADVISOR_STATE_PRESERVED = "preserved"

_ADVISORY_PREFIX = (
    "[Advisor] A cross-model reviewer raised the following while you were "
    "working. Weigh this evidence against your own; it is advice, not an "
    "instruction:\n"
)


@dataclass(frozen=True)
class AdvisoryEnvelope:
    """Typed identity and policy for one advisory delivery.

    ``note_text``/``evidence`` are the DISPLAY fields: the transcript card
    renders them structured, while the injected message keeps the
    weigh-not-obey framing the model needs. Both views come from the same
    validated note, so nothing is shown that was not delivered.
    """

    severity: str
    advisor_update_id: str
    reviewer_model: str
    note_text: str = ""
    evidence: str = ""

    def row_meta(self, state: str) -> dict[str, Any]:
        # The display fields are persisted to the transcript and rendered in
        # preference to the row content, so they carry the SAME outbound
        # redaction the injected message does -- reviewer output is model
        # output and can echo a credential its evidence tools read.
        return {
            "advisorSeverity": self.severity,
            "advisorUpdateId": self.advisor_update_id,
            "advisorModel": self.reviewer_model,
            "advisorState": state,
            "advisorText": _redact_outbound(self.note_text),
            "advisorEvidence": _redact_outbound(self.evidence),
        }


def _redact_outbound(text: str) -> str:
    """Both outbound redactors, in the delivery order used everywhere else."""
    if not text:
        return text
    from kiro_crew.security.exfil import redact_exfiltration_urls
    from kiro_crew.security.redaction import redact_credentials

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def advisory_message(note: AdvisorNote) -> str:
    """The steer text an advisory injects into the primary turn.

    The reviewer's text is MODEL OUTPUT and can echo a credential it read
    with its evidence tools, so it passes the same outbound redaction as
    every other provider-bound surface before injection or persistence.
    """
    evidence = f"\nEvidence: {note.evidence}" if note.evidence else ""
    body = _redact_outbound(f"[{note.severity}] {note.text}{evidence}")
    return f"{_ADVISORY_PREFIX}{body}"


async def deliver_advisory(
    state: "DashboardState",
    slot: "_ChatSlot",
    note: AdvisorNote,
    envelope: AdvisoryEnvelope,
) -> str:
    """Deliver *note* into the slot's running turn, or preserve it.

    Returns ``ADVISORY_STEERED`` when the live turn took the advisory, else
    ``ADVISORY_PRESERVED`` -- the advisory became a visible Advisor card and
    staged context for the next primary turn. There is no queue fallback by
    design.
    """
    # Local import: chat_delivery type-checks against dashboard state, and the
    # advisor package must stay importable without the dashboard loaded.
    from kiro_crew.dashboard.chat_delivery import (
        STEER_DISCARDED,
        STEER_STEERED,
        steer_into_running_turn,
    )

    message = advisory_message(note)
    outcome = await steer_into_running_turn(state, slot, message, envelope=envelope)
    if outcome == STEER_STEERED:
        return ADVISORY_STEERED
    if outcome == STEER_DISCARDED:
        # The user's hard kill explicitly discarded this advisory mid-flight;
        # preserving it would resurrect advice they threw away.
        logger.info("advisory %s discarded by hard kill", envelope.advisor_update_id)
        return ADVISORY_DISCARDED
    # Unavailable, refused, raced, or reconciled by the teardown: preserve.
    preserve_advisory(state, slot, message, envelope)
    return ADVISORY_PRESERVED


def preserve_advisory(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    envelope: AdvisoryEnvelope,
) -> None:
    """Persist *message* as a preserved Advisor card and stage its context.

    Idempotent per advisory update id: a delivery racing the turn teardown
    (both of which preserve on their own path) yields exactly one card and one
    staged context entry.
    """
    preserved: set[str] = getattr(slot, "_advisor_preserved_ids", set())
    if envelope.advisor_update_id in preserved:
        return
    preserved.add(envelope.advisor_update_id)
    from kiro_crew.dashboard.chat_delivery import sanitize_outbound

    sanitized = sanitize_outbound(message)
    # A steered advisory the turn never consumed already HAS its row: flip
    # that row to preserved instead of growing a duplicate card, but still
    # stage the context -- the primary model never saw the advice.
    for row in reversed(slot.messages):
        meta = row.get("meta") or {}
        if (
            row.get("role") == "advisor"
            and meta.get("advisorUpdateId") == envelope.advisor_update_id
        ):
            meta["advisorState"] = ADVISOR_STATE_PRESERVED
            # In-place row mutation: the dirty flush is what persists it, so a
            # clean slot at restart would resurrect the row as "steered".
            slot._dirty = True
            slot._advisor_pending_context.append(sanitized)
            # Live clients rendered this row as steered when it was pushed;
            # without a patch they keep that badge until reload.
            patch: dict = {"slot": slot.key, "ts": row.get("ts"), "meta": meta}
            mid = meta.get("mid")
            if isinstance(mid, str) and mid:
                patch["mid"] = mid
            try:
                state.broadcast_ws("chat_message_update", patch)
            except Exception:  # the row is already correct; clients reconcile
                logger.debug("advisor preserve patch broadcast failed", exc_info=True)
            logger.info(
                "advisor advisory %s preserved in place (steered row unconsumed)",
                envelope.advisor_update_id,
            )
            return
    ts = datetime.now(timezone.utc).isoformat()
    slot.append(
        "advisor",
        sanitized,
        "msg msg-advisor",
        ts=ts,
        meta=envelope.row_meta(ADVISOR_STATE_PRESERVED),
    )
    slot._advisor_pending_context.append(sanitized)
    logger.info(
        "advisory %s preserved for slot %s (severity=%s)",
        envelope.advisor_update_id,
        getattr(slot, "key", "?"),
        envelope.severity,
    )


def peek_pending_advisor_context(slot: "_ChatSlot") -> list[str]:
    """Read the staged advisor context WITHOUT clearing it.

    Non-destructive so a turn aborted before dispatch (stop, closing gate)
    keeps the advice for the next turn; the caller clears it with
    ``clear_pending_advisor_context`` only once the turn is accepted.
    """
    return list(getattr(slot, "_advisor_pending_context", []))


def clear_pending_advisor_context(slot: "_ChatSlot") -> None:
    """Clear staged advisor context after the turn was accepted. Idempotent."""
    staged = getattr(slot, "_advisor_pending_context", None)
    if staged:
        staged.clear()


def pop_pending_advisor_context(slot: "_ChatSlot") -> list[str]:
    """Drain the staged advisor context for the next primary turn (once)."""
    staged = peek_pending_advisor_context(slot)
    clear_pending_advisor_context(slot)
    return staged
