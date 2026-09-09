"""One-shot prompt surface: permission events are answered fail-closed.

The reviewer session runs unattended with no approval surface. A permission
request left unanswered stalls the review to its timeout; auto-approving
would grant an invisible escalation. The contract is REJECT + audit log.
"""

from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
)
from kiro_crew.agent_sdk.oneshot import prompt_for_reply


class _FakeHandle:
    def __init__(self, events):
        self._events = events
        self.rejected: list = []
        self.approved: list = []
        self.destroyed = False

    def prompt(self, _prompt):
        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def approve_tool(self, request_id, option_id=None):
        self.approved.append(request_id)

    async def destroy(self):
        self.destroyed = True


class _FakeRuntime:
    def __init__(self, handle):
        self._handle = handle

    async def create_session(self, *, cwd, agent):
        return self._handle


class TestPermissionEventsFailClosed:
    @pytest.mark.asyncio
    async def test_permission_request_is_rejected_and_stream_continues(self, caplog):
        events = [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="part1 "),
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="req-7", tool_kind="fs_read"),
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="part2"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        with caplog.at_level("WARNING"):
            reply = await prompt_for_reply(_FakeRuntime(handle), cwd=None, prompt="review this")
        # Fail-closed: rejected, never approved, and the stream still
        # completes so the reviewer replies with what it has.
        assert handle.rejected == ["req-7"]
        assert handle.approved == []
        assert reply.text == "part1 part2"
        assert reply.terminal is events[-1]
        assert any("permission" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_reject_failure_does_not_break_the_review(self):
        class _BrokenRejectHandle(_FakeHandle):
            async def reject_tool(self, request_id):
                raise RuntimeError("transport gone")

        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="req-8"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _BrokenRejectHandle(events)
        reply = await prompt_for_reply(_FakeRuntime(handle), cwd=None, prompt="p")
        assert reply.terminal is events[-1]


class TestServedModelSurfaced:
    """The reply carries the session's SERVED model so usage attribution is
    correct even when the configured reviewer model is empty (= inherit)."""

    @pytest.mark.asyncio
    async def test_reply_carries_served_model(self):
        events = [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        handle.served_model = "resolved-model-7"
        reply = await prompt_for_reply(_FakeRuntime(handle), cwd=None, prompt="p")
        assert reply.model == "resolved-model-7"

    @pytest.mark.asyncio
    async def test_absent_served_model_degrades_to_empty(self):
        events = [SimpleNamespace(kind=EVENT_COMPLETE)]
        reply = await prompt_for_reply(_FakeRuntime(_FakeHandle(events)), cwd=None, prompt="p")
        assert reply.model == ""


class TestPermissionRejectIsAudited:
    """Round-13: a permission DECISION must leave the same SEL record every
    other rejection path emits -- silence hides a security-relevant event."""

    @pytest.mark.asyncio
    async def test_reject_emits_sel_record(self, monkeypatch):
        records = []

        class _Sel:
            def log_api_access(self, **kw):
                records.append(kw)

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
        events = [
            SimpleNamespace(
                kind=EVENT_PERMISSION_REQUEST, request_id="req-9", tool_kind="execute_bash"
            ),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        await prompt_for_reply(_FakeRuntime(_FakeHandle(events)), cwd=None, prompt="p")
        assert len(records) == 1
        rec = records[0]
        assert rec["operation"] == "tool_permission"
        assert rec["outcome"] == "denied"
        assert "req-9" in rec["resources"]
