"""Advisor end-to-end: observation to guarded delivery through the pump.

Contract under test (see docs/system-specs/modules/advisor.md):

- ``extract_envelope`` tolerates a reviewer's final text as plain JSON, a
  fenced ```json block, or JSON embedded in prose; anything else raises the
  typed malformed error.
- ``render_update_prompt`` serializes an observation update with its
  identity (turn, epoch, seq, in-progress) and evidence.
- ``AdvisorService.pump_async`` drains the slot's observer, runs one bounded
  review through the pool, and dispatches the parsed result — the full
  observe -> review -> guard -> deliver path with a fake reviewer, no ACP.
- A reviewer failure degrades visibly and delivers nothing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.advisor.composition import extract_envelope, render_update_prompt
from kiro_crew.advisor.output import MalformedReviewerOutput
from kiro_crew.advisor.service import (
    AdvisorService,
    attach_for_turn,
    complete_turn,
    observe_segment,
    observe_tool_result,
)


class TestExtractEnvelope:
    def test_plain_json(self):
        notes = extract_envelope('{"version": 1, "notes": []}')
        assert notes == {"version": 1, "notes": []}

    def test_fenced_json(self):
        text = 'Here is my review:\n```json\n{"version": 1, "notes": [{"severity": "nit", "text": "x"}]}\n```\nDone.'
        env = extract_envelope(text)
        assert env["notes"][0]["severity"] == "nit"

    def test_json_embedded_in_prose(self):
        text = 'I looked carefully. {"version": 1, "notes": []} That is all.'
        assert extract_envelope(text)["version"] == 1

    @pytest.mark.parametrize("junk", ["", "no json here", "{broken", "[1,2,3]"])
    def test_junk_raises_typed_error(self, junk):
        with pytest.raises(MalformedReviewerOutput):
            extract_envelope(junk)


class TestRenderUpdatePrompt:
    def test_prompt_carries_identity_and_evidence(self):
        from kiro_crew.advisor.observation import AdvisorObserver

        obs = AdvisorObserver(parent_session_key="dashboard:a", turn_id="t1")
        obs.record_segment("I will delete the table.")
        obs.record_tool_result("fs_read", "schema body")
        update = obs.drain_update()
        prompt = render_update_prompt(update)
        assert "in progress" in prompt.lower()
        assert "I will delete the table." in prompt
        assert "fs_read" in prompt
        assert "schema body" in prompt
        assert str(update.seq) in prompt

    def test_final_update_is_marked_final(self):
        from kiro_crew.advisor.observation import AdvisorObserver

        obs = AdvisorObserver(parent_session_key="dashboard:a", turn_id="t1")
        obs.record_segment("final answer")
        update = obs.complete(stop_reason="end_turn")
        prompt = render_update_prompt(update)
        assert "final" in prompt.lower()
        assert "end_turn" in prompt


class FakePool:
    """Stands in for AdvisorReviewerRuntime: records reviews, returns canned text."""

    def __init__(self, result_text):
        self.result_text = result_text
        self.reviewed: list[tuple[str, dict]] = []
        self.statuses: dict[str, str] = {}

    async def acquire_session(self, key):
        return MagicMock(parent_session_key=key, session_id="advisor:9")

    async def review(self, key, payload):
        self.reviewed.append((key, payload))
        if isinstance(self.result_text, Exception):
            self.statuses[key] = "degraded"
            return None
        return self.result_text

    def status(self, key):
        return self.statuses.get(key, "watching")


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


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


def fresh_service(enabled=True) -> AdvisorService:
    import kiro_crew.advisor.service as service_mod

    service_mod._service = AdvisorService(enabled=enabled)
    return service_mod._service


class TestSchedulePump:
    @pytest.mark.asyncio
    async def test_disabled_service_schedules_nothing(self, state):
        service = fresh_service(enabled=False)
        service.set_reviewer_pool(FakePool('{"version": 1, "notes": []}'))
        from kiro_crew.advisor.service import schedule_pump

        slot = _running_slot(state)
        state._background_tasks = set()
        schedule_pump(state, slot)
        assert state._background_tasks == set()

    @pytest.mark.asyncio
    async def test_enabled_with_observer_schedules_one_pump(self, state):
        import asyncio

        service = fresh_service(enabled=True)
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        from kiro_crew.advisor.service import schedule_pump

        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "work happened")
        state._background_tasks = set()
        schedule_pump(state, slot)
        assert len(state._background_tasks) == 1
        await asyncio.gather(*state._background_tasks)
        assert len(pool.reviewed) == 1

    @pytest.mark.asyncio
    async def test_no_pool_schedules_nothing(self, state):
        service = fresh_service(enabled=True)
        from kiro_crew.advisor.service import schedule_pump

        slot = _running_slot(state)
        attach_for_turn(slot)
        # attach binds a pool lazily by design; the property pinned HERE is
        # the pump's robustness when no pool exists (bind failed / shut down).
        service._pool = None
        service._pool_model = ""
        state._background_tasks = set()
        schedule_pump(state, slot)
        assert state._background_tasks == set()


class TestPumpEndToEnd:
    @pytest.mark.asyncio
    async def test_blocker_flows_from_observation_to_advisor_row(self, state):
        service = fresh_service()
        envelope_text = json.dumps(
            {
                "version": 1,
                "notes": [{"severity": "blocker", "text": "dropping the wrong table"}],
            }
        )
        service.set_reviewer_pool(FakePool(envelope_text))
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "I will drop table users_prod.")
        observe_tool_result(slot, "fs_read", "schema")
        await service.pump_async(state, slot)
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorSeverity"] == "blocker"

    @pytest.mark.asyncio
    async def test_pump_with_nothing_recorded_reviews_nothing(self, state):
        service = fresh_service()
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        await service.pump_async(state, slot)
        assert pool.reviewed == []

    @pytest.mark.asyncio
    async def test_final_update_flows_after_complete(self, state):
        service = fresh_service()
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "the answer")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert len(pool.reviewed) == 1
        assert pool.reviewed[0][1]["in_progress"] is False

    @pytest.mark.asyncio
    async def test_reviewer_failure_delivers_nothing(self, state):
        service = fresh_service()
        service.set_reviewer_pool(FakePool(RuntimeError("boom")))
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "text")
        before = len(slot.messages)
        await service.pump_async(state, slot)
        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_disabled_service_pump_is_inert(self, state):
        service = fresh_service(enabled=False)
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        await service.pump_async(state, slot)
        assert pool.reviewed == []


class TestPumpWorkdirSupply:
    @pytest.mark.asyncio
    async def test_pump_passes_the_slots_project_as_work_dir(self, state):
        service = fresh_service()
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        slot.project = "/parent/project"
        attach_for_turn(slot)
        observe_segment(slot, "text")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert pool.reviewed[0][1]["work_dir"] == "/parent/project"


class TestEffectiveEnablementPump:
    """Round-6: the pump must honor the ATTACHED observer, not the global
    flag -- a session opted ON under a global-off default gets reviews."""

    @pytest.mark.asyncio
    async def test_override_on_under_global_off_still_pumps(self, state):
        service = fresh_service(enabled=False)  # global default OFF
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        slot.advisor_override = "on"
        assert attach_for_turn(slot) is not None
        observe_segment(slot, "work under per-session opt-in")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert len(pool.reviewed) == 1, "per-session opt-in must review under a global-off default"

    @pytest.mark.asyncio
    async def test_stale_review_never_dispatches_across_an_epoch_boundary(self, state):
        import asyncio

        service = fresh_service(enabled=True)

        class SlowPool(FakePool):
            def __init__(self, text):
                super().__init__(text)
                self.gate = asyncio.Event()

            async def review(self, key, payload):
                await self.gate.wait()
                return await super().review(key, payload)

        pool = SlowPool('{"version": 1, "notes": [{"severity": "nit", "text": "stale advice"}]}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "pre-reset work")
        complete_turn(slot, stop_reason="end_turn")
        task = asyncio.create_task(service.pump_async(state, slot))
        await asyncio.sleep(0)  # pump is now awaiting the slow review
        service.notify_boundary(f"dashboard:{slot.key}", "reset")  # epoch moves
        pool.gate.set()
        await task
        advisor_rows = [m for m in slot.messages if m.get("role") == "advisor"]
        assert advisor_rows == [], "a review raced by a reset must not dispatch into the new epoch"


class TestReviewThrottle:
    """Live-run finding: every checkpoint spawned a review (20 per turn).
    In-progress reviews are throttled per session; the final update always
    reviews."""

    @pytest.mark.asyncio
    async def test_rapid_checkpoints_yield_one_inflight_review(self, state):
        service = fresh_service(enabled=True)
        service.review_min_interval_secs = 300.0
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "one")
        await service.pump_async(state, slot)
        observe_segment(slot, "two")
        await service.pump_async(state, slot)  # throttled
        observe_segment(slot, "three")
        await service.pump_async(state, slot)  # throttled
        assert len(pool.reviewed) == 1

    @pytest.mark.asyncio
    async def test_final_update_reviews_despite_throttle(self, state):
        service = fresh_service(enabled=True)
        service.review_min_interval_secs = 300.0
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "one")
        await service.pump_async(state, slot)
        observe_segment(slot, "two")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert len(pool.reviewed) == 2, "the final update must always review"


class TestLateReviewPreservesInsteadOfSteering:
    """Round-10: a review finishing after the NEXT turn began must not steer
    stale advice into the successor turn -- it preserves (card + context)."""

    @pytest.mark.asyncio
    async def test_epoch_advance_during_review_forces_preserve(self, state):
        service = fresh_service(enabled=True)
        envelope_text = json.dumps(
            {"version": 1, "notes": [{"severity": "blocker", "text": "stale blocker"}]}
        )
        slot = _running_slot(state)
        steered = []

        async def record_steer(message):
            steered.append(message)
            return True

        slot._acp_client.steer = record_steer

        class _SlowPool(FakePool):
            async def review(self, session_key, payload):
                # The next turn begins while the review is in flight: the
                # observer re-primes onto a new epoch (same conversation, no
                # boundary), so the advice now describes a finished turn.
                service._observers[session_key].begin_turn()
                return await super().review(session_key, payload)

        service.set_reviewer_pool(_SlowPool(envelope_text))
        observer = attach_for_turn(slot)
        observe_segment(slot, "I will drop table users_prod.")
        observe_tool_result(slot, "fs_read", "schema")
        observer.complete()  # the turn finished; final review is due
        await service.pump_async(state, slot)
        # never steered into the successor turn; preserved card + context
        assert steered == []
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorState"] == "preserved"
        assert slot._advisor_pending_context


class TestAcquireFailurePreservesUpdate:
    """Round-13: a runtime spawn failure at acquire must neither crash the
    pump task nor consume the drained update -- the next pump retries it."""

    @pytest.mark.asyncio
    async def test_failed_acquire_keeps_the_completed_update(self, state):
        service = fresh_service(enabled=True)

        class _BrokenPool(FakePool):
            async def acquire_session(self, key):
                raise RuntimeError("spawn failed")

        service.set_reviewer_pool(_BrokenPool("{}"))
        slot = _running_slot(state)
        observer = attach_for_turn(slot)
        observe_segment(slot, "some work")
        observer.complete()
        # must not raise
        await service.pump_async(state, slot)
        # the final update survives for a later retry
        assert observer.take_completed() is not None


class TestRebindDuringReviewDiscards:
    """Round-14: a slot rebound to a DIFFERENT conversation mid-review must
    not receive the prior session's advice -- the post-review identity check
    recomputes the slot's effective key."""

    @pytest.mark.asyncio
    async def test_linked_session_change_discards_the_review(self, state):
        service = fresh_service(enabled=True)
        envelope_text = json.dumps(
            {"version": 1, "notes": [{"severity": "blocker", "text": "old-session advice"}]}
        )
        slot = _running_slot(state)

        class _RebindPool(FakePool):
            async def review(self, session_key, payload):
                # A cron/workflow rebind lands while the review is in flight:
                # the slot now fronts a different conversation.
                slot.linked_session_key = "slack:99999.111"
                return await super().review(session_key, payload)

        service.set_reviewer_pool(_RebindPool(envelope_text))
        observer = attach_for_turn(slot)
        observe_segment(slot, "work in the ORIGINAL conversation")
        observer.complete()
        await service.pump_async(state, slot)
        # nothing persisted or staged into the rebound conversation
        assert not [m for m in slot.messages if m.get("role") == "advisor"]
        assert not getattr(slot, "_advisor_pending_context", [])
