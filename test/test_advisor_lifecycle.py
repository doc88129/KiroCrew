"""Advisor lifecycle integration: epochs join the parent's real boundaries.

Contract under test (see docs/system-specs/modules/advisor.md):

- ``AdvisorService.notify_boundary(key, reason)`` starts a new observation
  epoch for epoch-scoped reasons (reset, compaction, history rewrite) and
  detaches the observer for terminal reasons (close, remove, transfer).
- The dashboard's single reset choke point (``_reset_slot_session`` -- the
  helper every agent/model/bulk/effort/workspace switch and reload routes
  through) notifies the advisor service.
- ``close_slot`` notifies with a terminal reason.
- ``dispose_all`` drops every observer (gateway shutdown).
- A disabled service ignores boundary notifications without error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.advisor.service import (
    BOUNDARY_CLOSE,
    BOUNDARY_COMPACTION,
    BOUNDARY_RESET,
    AdvisorService,
)


class TestServiceBoundarySemantics:
    def test_reset_boundary_begins_new_epoch(self):
        service = AdvisorService(enabled=True)
        observer = service.attach("dashboard:a")
        observer.record_tool_result("read", "stale evidence")
        service.notify_boundary("dashboard:a", BOUNDARY_RESET)
        assert service.observer_count() == 1
        assert observer.drain_update() is None, "pending records must not cross"

    def test_compaction_boundary_begins_new_epoch(self):
        service = AdvisorService(enabled=True)
        observer = service.attach("dashboard:a")
        observer.record_segment("pre-compaction text")
        service.notify_boundary("dashboard:a", BOUNDARY_COMPACTION)
        assert observer.drain_update() is None

    def test_close_boundary_detaches(self):
        service = AdvisorService(enabled=True)
        service.attach("dashboard:a")
        service.notify_boundary("dashboard:a", BOUNDARY_CLOSE)
        assert service.observer_count() == 0

    def test_unknown_session_boundary_is_a_noop(self):
        service = AdvisorService(enabled=True)
        service.notify_boundary("dashboard:ghost", BOUNDARY_RESET)
        assert service.observer_count() == 0

    def test_disabled_service_ignores_boundaries(self):
        service = AdvisorService(enabled=False)
        service.notify_boundary("dashboard:a", BOUNDARY_RESET)
        service.notify_boundary("dashboard:a", BOUNDARY_CLOSE)
        assert service.observer_count() == 0

    def test_dispose_all_drops_every_observer(self):
        service = AdvisorService(enabled=True)
        service.attach("dashboard:a")
        service.attach("dashboard:b")
        service.dispose_all()
        assert service.observer_count() == 0


class TestDashboardChokePoints:
    """The real call sites notify the advisor service."""

    @pytest.fixture
    def state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        st = _make_state(tmp_path)
        st.broadcast_ws = MagicMock()
        return st

    @pytest.fixture
    def recording_service(self, monkeypatch):
        service = AdvisorService(enabled=True)
        calls: list[tuple[str, str]] = []
        original = service.notify_boundary

        def recording(key: str, reason: str) -> None:
            calls.append((key, reason))
            original(key, reason)

        service.notify_boundary = recording  # type: ignore[method-assign]
        monkeypatch.setattr("kiro_crew.advisor.service.get_advisor_service", lambda: service)
        service.calls = calls  # type: ignore[attr-defined]
        return service

    @pytest.mark.asyncio
    async def test_reset_slot_session_notifies_reset_boundary(self, state, recording_service):
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        slot = state.get_or_create_slot("test")
        state.sessions.reset = AsyncMock(return_value=True)
        await _reset_slot_session(state, slot, "dashboard:test")
        assert ("dashboard:test", BOUNDARY_RESET) in recording_service.calls

    @pytest.mark.asyncio
    async def test_close_slot_notifies_close_boundary(self, state, recording_service):
        from kiro_crew.dashboard.chat_handlers import close_slot

        slot = state.get_or_create_slot("test")
        await close_slot(state, slot, "test")
        closes = [c for c in recording_service.calls if c[1] == BOUNDARY_CLOSE]
        assert closes, "close_slot must notify a terminal advisor boundary"

    @pytest.mark.asyncio
    async def test_successful_compaction_notifies_compaction_boundary(
        self, state, recording_service
    ):
        state.wire_session_compact_callback()
        callback = state.sessions.set_compact_callback.call_args[0][0]
        state.get_or_create_slot("test")
        await callback("dashboard:test", 87.0, success=True)
        assert ("dashboard:test", BOUNDARY_COMPACTION) in recording_service.calls

    @pytest.mark.asyncio
    async def test_failed_compaction_does_not_touch_the_epoch(self, state, recording_service):
        state.wire_session_compact_callback()
        callback = state.sessions.set_compact_callback.call_args[0][0]
        state.get_or_create_slot("test")
        await callback("dashboard:test", 87.0, success=False)
        compactions = [c for c in recording_service.calls if c[1] == BOUNDARY_COMPACTION]
        assert compactions == [], "a failed compact rewrote nothing"


class TestPoolLifecycleWiring:
    """Round-4: the reap half of the pool must be reachable in production.

    A terminal boundary releases the parent's reviewer session, and gateway
    disposal shuts the pool down so the shared subprocess dies with the
    gateway instead of orphaning.
    """

    @pytest.mark.asyncio
    async def test_terminal_boundary_releases_the_pool_session(self):
        import asyncio  # noqa: F811

        service = AdvisorService(enabled=True)

        class FakePool:
            def __init__(self):
                self.released = []

            async def release_session(self, key):
                self.released.append(key)

            async def shutdown(self):
                pass

        pool = FakePool()
        service.set_reviewer_pool(pool)
        service.attach("dashboard:a")
        service.notify_boundary("dashboard:a", "close")
        await asyncio.sleep(0)  # let the scheduled release run
        assert pool.released == ["dashboard:a"]

    @pytest.mark.asyncio
    async def test_dispose_all_schedules_pool_shutdown(self):
        import asyncio  # noqa: F811

        service = AdvisorService(enabled=True)

        class FakePool:
            def __init__(self):
                self.shut = False

            async def release_session(self, key):
                pass

            async def shutdown(self):
                self.shut = True

        pool = FakePool()
        service.set_reviewer_pool(pool)
        service.dispose_all()
        await asyncio.sleep(0)
        assert pool.shut is True
