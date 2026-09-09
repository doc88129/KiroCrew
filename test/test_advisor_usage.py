"""Advisor usage attribution: reviewer rows correlate to their parent.

Contract under test (see docs/system-specs/modules/advisor.md):

- The token-record schema gains OPTIONAL ``parent_session_key`` and
  ``advisor_update_id`` fields. Absent (every existing caller), the record's
  key set is unchanged -- additive schema, byte-compatible rows.
- An advisor reviewer usage row is keyed by the reviewer session's own stable
  synthetic key (``advisor:<n>``), tagged ``surface="advisor"``, and carries
  the explicit parent link -- it is never attributed to the parent session
  key itself and never appears as a user turn.
- ``advisor_usage_kwargs`` composes exactly those fields from a
  ``ReviewerSession``.
"""

from __future__ import annotations

from datetime import datetime

from kiro_crew.acp.types import TurnUsage
from kiro_crew.advisor.runtime import ReviewerSession
from kiro_crew.advisor.usage import advisor_usage_kwargs
from kiro_crew.dashboard.handlers.usage import _build_token_record


def _event(tokens_in=100, tokens_out=20):
    return TurnUsage(input_tokens=tokens_in, output_tokens=tokens_out)


class TestSchemaAdditivity:
    def test_record_without_advisor_fields_has_unchanged_keys(self):
        base = _build_token_record(
            "dashboard:a", "model-x", _event(), "acp", datetime.now().astimezone()
        )
        assert "parent_session_key" not in base
        assert "advisor_update_id" not in base

    def test_record_with_advisor_fields_carries_both(self):
        rec = _build_token_record(
            "advisor:7",
            "model-x",
            _event(),
            "acp",
            datetime.now().astimezone(),
            surface="advisor",
            parent_session_key="dashboard:a",
            advisor_update_id="adv-42",
        )
        assert rec["parent_session_key"] == "dashboard:a"
        assert rec["advisor_update_id"] == "adv-42"
        assert rec["surface"] == "advisor"
        assert rec["slot"] == "advisor:7"

    def test_empty_advisor_fields_are_omitted_not_written(self):
        rec = _build_token_record(
            "dashboard:a",
            "model-x",
            _event(),
            "acp",
            datetime.now().astimezone(),
            parent_session_key="",
            advisor_update_id="",
        )
        assert "parent_session_key" not in rec
        assert "advisor_update_id" not in rec


class TestAdvisorUsageKwargs:
    def test_kwargs_compose_reviewer_identity_and_parent_link(self):
        session = ReviewerSession("dashboard:a")
        kwargs = advisor_usage_kwargs(session, advisor_update_id="adv-9")
        assert kwargs["slot_key"] == session.session_id
        assert kwargs["slot_key"].startswith("advisor:")
        assert kwargs["surface"] == "advisor"
        assert kwargs["parent_session_key"] == "dashboard:a"
        assert kwargs["advisor_update_id"] == "adv-9"

    def test_reviewer_key_is_stable_per_session(self):
        session = ReviewerSession("dashboard:a")
        first = advisor_usage_kwargs(session, advisor_update_id="u1")["slot_key"]
        second = advisor_usage_kwargs(session, advisor_update_id="u2")["slot_key"]
        assert first == second
