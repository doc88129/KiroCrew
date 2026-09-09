"""Per-slot advisor override: slot field, projection, endpoint, persistence.

Contract under test (see docs/system-specs/modules/advisor.md and the
reasoning-effort precedent this mirrors):

- A fresh slot's ``advisor_override`` is ``inherit``.
- The slot projection carries the field to slots/SSE/WebSocket consumers.
- ``POST /api/chat/slots/{slot}/advisor-override`` accepts exactly
  ``inherit|on|off``, rejects anything else with 400 (slot unchanged), and
  pushes one slots update.
- The value round-trips through JSONL history save/restore; ``inherit`` is
  persisted explicitly (it is a non-empty clear value); an invalid persisted
  value restores as ``inherit``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


def _make_app_with_override(state):
    """The minimal chat app plus the advisor-override route under test."""
    from kiro_crew.dashboard.chat import api_chat_slot_advisor_override

    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/advisor-override", api_chat_slot_advisor_override)
    return app


@pytest.fixture
def _patch_sel():
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


class TestSlotFieldAndProjection:
    def test_fresh_slot_defaults_to_inherit(self, state):
        slot = state.get_or_create_slot("test")
        assert slot.advisor_override == "inherit"

    def test_slots_payload_carries_the_field(self, state):
        slot = state.get_or_create_slot("test")
        slot.advisor_override = "on"
        projected = slot.to_dict()
        assert projected["advisor_override"] == "on"


class TestAdvisorOverrideEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["on", "off", "inherit"])
    async def test_sets_valid_values(self, state, _patch_sel, value):
        slot = state.get_or_create_slot("test")
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/advisor-override",
                json={"advisor_override": value},
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["advisor_override"] == value
        assert slot.advisor_override == value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["banana", "ON", "", 42, None, ["on"]])
    async def test_rejects_invalid_values(self, state, _patch_sel, bad):
        slot = state.get_or_create_slot("test")
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/advisor-override",
                json={"advisor_override": bad},
            )
            assert resp.status == 400
        assert slot.advisor_override == "inherit", "a rejected value must not land"

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, state, _patch_sel):
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/ghost/advisor-override",
                json={"advisor_override": "on"},
            )
            assert resp.status == 404


class TestPersistenceRoundTrip:
    def test_validator_accepts_closed_set_and_falls_back(self):
        from kiro_crew.dashboard.chat_persistence import _validate_advisor_override

        assert _validate_advisor_override("on") == "on"
        assert _validate_advisor_override("off") == "off"
        assert _validate_advisor_override("inherit") == "inherit"
        for bad in ("banana", "", None, 42, "ON"):
            assert _validate_advisor_override(bad) == "inherit"

    def test_override_survives_save_and_rehydrate(self, state, tmp_path):
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        slot = state.get_or_create_slot("test")
        slot.advisor_override = "on"
        slot.append("user", "hello", "msg msg-u")
        _save_slot_to_history(state, slot)

        # Drop the live slot so rehydrate builds a fresh one from disk.
        state._slots.pop("test", None)
        restored_slot = _rehydrate_slot_from_history(state, "test")
        assert restored_slot is not None
        assert state.get_or_create_slot("test").advisor_override == "on"


class TestOverrideReauthorizedInsideLock:
    """Round-14: the body-read await lets the slot be replaced/rebound; the
    assignment must reauthorize against the CURRENT binding inside the lock."""

    def test_reauthorization_is_inside_the_lock(self):
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat_slot_advisor_override)
        lock_at = src.find("async with slot._lock:")
        assert lock_at != -1
        inside = src[lock_at:]
        # identity re-check and the cross-app denial both live inside the lock
        assert "state._slots.get(name)" in inside
        assert "_deny_cross_app_slot_access" in inside
        assert inside.find("_deny_cross_app_slot_access") < inside.find(
            "slot.advisor_override = override"
        )
