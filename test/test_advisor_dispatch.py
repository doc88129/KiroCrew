"""Advisor dispatch: reviewer envelope through guard and severity routing.

Contract under test (see docs/system-specs/modules/advisor.md):

- ``dispatch_reviewer_result`` validates the envelope, admits notes through
  the emission guard, and routes by severity: a ``blocker`` goes to advisory
  delivery (steer or preserve); ``nit``/``concern`` become preserved Advisor
  cards + pending context, never steers.
- Malformed reviewer output degrades the session status and delivers
  nothing; the primary is never affected.
- Guard-suppressed notes deliver nothing.
- The final (``in_progress=False``) update resets the guard's per-update
  budget via ``begin_update`` on the next dispatch.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.advisor.composition import AdvisorDispatcher
from kiro_crew.advisor.guard import EmissionGuard


def _running_slot(state, key="test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = MagicMock()
    client.supports_steer = True

    async def accept(message):
        return True

    client.steer = accept
    slot._acp_client = client
    return slot


def envelope(notes):
    return {"version": 1, "notes": notes}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


@pytest.fixture
def dispatcher():
    return AdvisorDispatcher(guard=EmissionGuard(), reviewer_model="rev-x")


class TestSeverityRouting:
    @pytest.mark.asyncio
    async def test_blocker_is_steered_into_running_turn(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "blocker", "text": "wrong table dropped"}]),
            advisor_update_id="u1",
        )
        assert outcome == {"steered": 1}
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorSeverity"] == "blocker"

    @pytest.mark.asyncio
    async def test_nit_becomes_preserved_card_never_steer(self, state, dispatcher):
        slot = _running_slot(state)
        steer_calls = []

        async def record_steer(message):
            steer_calls.append(message)
            return True

        slot._acp_client.steer = record_steer
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "nit", "text": "typo in comment"}]),
            advisor_update_id="u2",
        )
        assert outcome == {"preserved": 1}
        assert steer_calls == []
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorState"] == "preserved"

    @pytest.mark.asyncio
    async def test_concern_is_preserved(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "concern", "text": "unbounded retry"}]),
            advisor_update_id="u3",
        )
        assert outcome == {"preserved": 1}

    @pytest.mark.asyncio
    async def test_mixed_severities_route_independently(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope(
                [
                    {"severity": "nit", "text": "naming"},
                    {"severity": "blocker", "text": "data loss"},
                ]
            ),
            advisor_update_id="u4",
        )
        assert outcome == {"preserved": 1, "steered": 1}


class TestGuardAndDegradation:
    @pytest.mark.asyncio
    async def test_malformed_envelope_degrades_and_delivers_nothing(self, state, dispatcher):
        slot = _running_slot(state)
        before = len(slot.messages)
        outcome = await dispatcher.dispatch(state, slot, "free-form prose", advisor_update_id="u5")
        assert outcome == {"degraded": 1}
        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_suppressed_duplicate_delivers_nothing(self, state, dispatcher):
        slot = _running_slot(state)
        note = [{"severity": "concern", "text": "same finding"}]
        await dispatcher.dispatch(state, slot, envelope(note), advisor_update_id="u6")
        before = len(slot.messages)
        outcome = await dispatcher.dispatch(state, slot, envelope(note), advisor_update_id="u7")
        assert outcome == {}
        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_empty_notes_is_a_clean_noop(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(state, slot, envelope([]), advisor_update_id="u8")
        assert outcome == {}


class TestAdvisorAgentMaterialization:
    """The packaged reviewer agent spec reaches the kiro agents dir.

    Found by the live-gateway e2e: the runtime spawns with
    agent="kirocrew-advisor", which only resolves if the packaged JSON has
    been materialized into the agents dir.
    """

    def test_installs_the_packaged_spec_when_absent(self, tmp_path):
        import json

        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        installed = ensure_advisor_agent_installed(agents_dir=tmp_path)
        assert installed == tmp_path / "kirocrew-advisor.json"
        spec = json.loads(installed.read_text())
        assert spec["name"] == "kirocrew-advisor"
        assert spec["tools"] == ["fs_read", "grep", "glob"]

    def test_does_not_overwrite_an_existing_spec(self, tmp_path):
        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        target = tmp_path / "kirocrew-advisor.json"
        target.write_text('{"name": "kirocrew-advisor", "user": "edited"}')
        ensure_advisor_agent_installed(agents_dir=tmp_path)
        assert "edited" in target.read_text()

    def test_is_idempotent(self, tmp_path):
        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        first = ensure_advisor_agent_installed(agents_dir=tmp_path)
        second = ensure_advisor_agent_installed(agents_dir=tmp_path)
        assert first == second

    def test_existing_spec_allowed_tools_are_ceiling_filtered(self, tmp_path, monkeypatch):
        """An on-disk spec is a persistent artifact: a ceiling-denied
        `allowedTools` grant in it must be withheld at every launch, or the
        spec becomes a one-time escalation that outlives the policy."""
        import json

        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        target = tmp_path / "kirocrew-advisor.json"
        target.write_text(
            json.dumps(
                {
                    "name": "kirocrew-advisor",
                    "user": "edited",
                    "allowedTools": ["fs_read", "execute_bash"],
                }
            )
        )
        monkeypatch.setattr("kiro_crew.agent._may_auto_approve", lambda ref: ref == "fs_read")
        ensure_advisor_agent_installed(agents_dir=tmp_path)
        spec = json.loads(target.read_text())
        # user edit preserved, denied grant withheld
        assert spec["user"] == "edited"
        assert spec["allowedTools"] == ["fs_read"]

    def test_fresh_install_is_ceiling_filtered_too(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        monkeypatch.setattr("kiro_crew.agent._may_auto_approve", lambda ref: False)
        installed = ensure_advisor_agent_installed(agents_dir=tmp_path)
        spec = json.loads(installed.read_text())
        assert spec.get("allowedTools", []) == []


class TestReviewerModelAndWorkdirPlumbing:
    """Round-4: advisor.model must select the reviewer model, and each
    reviewer session must run in the observed slot's own workspace."""

    def test_reviewer_model_reaches_the_runtime(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        def fake_create(*, agent, work_dir, model=None, **kw):
            seen["agent"] = agent
            seen["model"] = model
            return object()

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.create_agent_runtime", fake_create)
        pool = comp.build_reviewer_runtime("reviewer-x", work_dir=None)
        pool._runtime_factory()
        assert seen["model"] == "reviewer-x"
        assert seen["agent"] == comp.ADVISOR_AGENT_NAME

    def test_empty_reviewer_model_selects_runtime_default(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        def fake_create(*, agent, work_dir, model=None, **kw):
            seen["model"] = model
            return object()

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.create_agent_runtime", fake_create)
        comp.build_reviewer_runtime("", work_dir=None)._runtime_factory()
        assert seen["model"] is None

    @pytest.mark.asyncio
    async def test_prompt_fn_uses_the_payloads_work_dir(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        async def fake_prompt_for_reply(runtime, *, cwd, prompt):
            seen["cwd"] = cwd
            from kiro_crew.agent_sdk.oneshot import OneShotReply

            return OneShotReply("ok", None)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        pool = comp.build_reviewer_runtime("m", work_dir="/gateway/cwd")
        out = await pool._prompt_fn(
            object(),
            {"_runtime": object(), "prompt": "p", "work_dir": "/parent/project"},
        )
        assert out == "ok"
        assert (
            seen["cwd"] == "/parent/project"
        ), "the reviewer session must run in the observed slot's workspace"

    @pytest.mark.asyncio
    async def test_prompt_fn_falls_back_to_the_pool_work_dir(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        async def fake_prompt_for_reply(runtime, *, cwd, prompt):
            seen["cwd"] = cwd
            from kiro_crew.agent_sdk.oneshot import OneShotReply

            return OneShotReply("ok", None)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        pool = comp.build_reviewer_runtime("m", work_dir="/gateway/cwd")
        await pool._prompt_fn(object(), {"_runtime": object(), "prompt": "p"})
        assert seen["cwd"] == "/gateway/cwd"


class TestReviewerUsagePersistence:
    """Round-4: reviewer spend must actually be persisted (FP blocker 3)."""

    @pytest.mark.asyncio
    async def test_reviewer_turn_persists_an_attributed_usage_row(self, monkeypatch):
        import kiro_crew.advisor.composition as comp
        from kiro_crew.advisor.runtime import ReviewerSession

        terminal = object()

        class Reply:
            text = '{"version": 1, "notes": []}'
            model = "served-model-y"  # the backend-resolved id wins attribution

        Reply.terminal = terminal

        async def fake_prompt_for_reply(runtime, *, cwd, prompt):
            return Reply

        persisted = {}

        async def fake_persist(slot_key, model, event, provider="", **kw):
            persisted["slot_key"] = slot_key
            persisted["model"] = model
            persisted["event"] = event
            persisted.update(kw)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.usage.persist_token_record_async",
            fake_persist,
        )
        pool = comp.build_reviewer_runtime("reviewer-x", work_dir=None)
        session = ReviewerSession(parent_session_key="dashboard:p")
        out = await pool._prompt_fn(
            session,
            {
                "_runtime": object(),
                "prompt": "p",
                "advisor_update_id": "dashboard:p:2:7",
            },
        )
        assert out == Reply.text
        assert persisted["slot_key"] == session.session_id
        assert persisted["slot_key"].startswith("advisor:")
        assert persisted["model"] == "served-model-y"  # served id outranks configured
        assert persisted["event"] is terminal
        assert persisted["surface"] == "advisor"
        assert persisted["parent_session_key"] == "dashboard:p"
        assert persisted["advisor_update_id"] == "dashboard:p:2:7"

    @pytest.mark.asyncio
    async def test_usage_persistence_failure_never_breaks_the_review(self, monkeypatch):
        import kiro_crew.advisor.composition as comp
        from kiro_crew.advisor.runtime import ReviewerSession

        class Reply:
            text = "raw"
            terminal = object()

        async def fake_prompt_for_reply(runtime, *, cwd, prompt):
            return Reply

        async def broken_persist(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.usage.persist_token_record_async",
            broken_persist,
        )
        pool = comp.build_reviewer_runtime("m", work_dir=None)
        session = ReviewerSession(parent_session_key="dashboard:p")
        out = await pool._prompt_fn(
            session, {"_runtime": object(), "prompt": "p", "advisor_update_id": "x"}
        )
        assert out == "raw"
