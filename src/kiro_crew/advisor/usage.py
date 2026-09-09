"""Advisor usage attribution helpers.

A reviewer turn is real model spend and must be visible, but it is not the
parent's turn: the row is keyed by the reviewer session's own stable
synthetic key (``advisor:<n>``), tagged with the ``advisor`` surface, and
carries the parent link explicitly through the additive
``parent_session_key`` / ``advisor_update_id`` record fields.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.advisor.runtime import ReviewerSession

#: Dispatch-origin tag advisor reviewer rows carry in the usage store.
ADVISOR_USAGE_SURFACE = "advisor"


def advisor_usage_kwargs(session: ReviewerSession, *, advisor_update_id: str) -> dict[str, Any]:
    """Keyword arguments for persisting one reviewer turn's usage row.

    Spread into ``persist_token_record_async`` alongside the model/event the
    reviewer turn produced.
    """
    return {
        "slot_key": session.session_id,
        "surface": ADVISOR_USAGE_SURFACE,
        "parent_session_key": session.parent_session_key,
        "advisor_update_id": advisor_update_id,
    }
