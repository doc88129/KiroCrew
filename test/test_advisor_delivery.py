"""Advisory delivery: typed envelope over the existing steer ledger.

Contract under test (see docs/system-specs/modules/advisor.md):

- An advisory delivery reuses the SAME steer ledger as user sends (pending
  registration, delivery ids, consumption evidence, teardown reconciliation)
  -- no parallel ledger, no direct ACP calls.
- The persisted advisory row carries the ``advisor`` role and provenance meta
  (severity, advisor update id, reviewer model), never the user role.
- User sends without an envelope stay byte-compatible: same row role, same
  meta keys, no advisor fields.
- preserve-on-unconsumed: an advisory steer the turn never consumed is
  converted at teardown into a preserved Advisor card plus staged pending
  context. It MUST NOT enter the user queue or execute as a user-authored
  turn. A user steer in the same teardown is still requeued normally.
- When steering is unavailable, an advisory is preserved immediately (card +
  pending context), never queued.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.advisor.delivery import (
    ADVISORY_PRESERVED,
    ADVISORY_STEERED,
    AdvisoryEnvelope,
    advisory_message,
    deliver_advisory,
    pop_pending_advisor_context,
    preserve_advisory,
)
from kiro_crew.advisor.output import AdvisorNote
from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers


def _running_slot(state, key="test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _steer_client(accept=True):
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=accept)
    return client


def make_envelope(**kwargs):
    defaults = dict(
        severity="blocker",
        advisor_update_id="adv-1",
        reviewer_model="reviewer-model-x",
    )
    defaults.update(kwargs)
    return AdvisoryEnvelope(**defaults)


def make_note(severity="blocker", text="the fix deletes the wrong table"):
    return AdvisorNote(severity=severity, text=text)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


class TestUserSendByteCompatibility:
    @pytest.mark.asyncio
    async def test_user_steer_row_shape_is_unchanged(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        outcome = await steer_into_running_turn(state, slot, "fix the test")
        assert outcome == "steered"
        row = slot.messages[-1]
        assert row["role"] == "user"
        meta = row["meta"]
        assert meta.get("steer") is True
        assert "steerState" in meta
        # No advisor contamination on the user path.
        assert not any(k.startswith("advisor") for k in meta)

    @pytest.mark.asyncio
    async def test_user_steer_teardown_still_requeues_to_user_queue(self, state):
        slot = _running_slot(state)
        slot._pending_steers.append("user message that raced the end")
        _requeue_unconsumed_steers(state, slot)
        assert len(slot._queue) == 1
        assert slot._queue[0]["content"] == "user message that raced the end"


class TestAdvisoryRow:
    @pytest.mark.asyncio
    async def test_advisory_steer_persists_advisor_role_row(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        outcome = await deliver_advisory(state, slot, make_note(), make_envelope())
        assert outcome == ADVISORY_STEERED
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        meta = row["meta"]
        assert meta["advisorSeverity"] == "blocker"
        assert meta["advisorUpdateId"] == "adv-1"
        assert meta["advisorModel"] == "reviewer-model-x"
        assert meta["advisorState"] == "steered"

    @pytest.mark.asyncio
    async def test_advisory_uses_the_same_steer_ledger(self, state):
        """No parallel ledger: the advisory in-flight guard is the same map."""
        slot = _running_slot(state)

        seen_pending = {}

        async def capture_steer(message):
            seen_pending["registered"] = message in slot._pending_steers
            return True

        client = MagicMock()
        client.supports_steer = True
        client.steer = AsyncMock(side_effect=capture_steer)
        slot._acp_client = client
        await deliver_advisory(state, slot, make_note(), make_envelope())
        assert seen_pending["registered"] is True

    @pytest.mark.asyncio
    async def test_advisory_text_tells_primary_to_weigh_not_obey(self, state):
        slot = _running_slot(state)
        client = _steer_client(accept=True)
        slot._acp_client = client
        await deliver_advisory(state, slot, make_note(), make_envelope())
        sent = client.steer.call_args[0][0]
        assert "weigh" in sent.lower()


class TestPreserveOnUnconsumed:
    @pytest.mark.asyncio
    async def test_unconsumed_advisory_becomes_preserved_card_not_queue(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        await deliver_advisory(state, slot, make_note(), make_envelope())
        # The turn ends without a consumption echo: teardown reconciles.
        _requeue_unconsumed_steers(state, slot)
        assert slot._queue == [], "advisory must never enter the user queue"
        preserved = [
            m
            for m in slot.messages
            if isinstance(m.get("meta"), dict) and m["meta"].get("advisorState") == "preserved"
        ]
        assert len(preserved) == 1
        assert preserved[0]["role"] == "advisor"

    @pytest.mark.asyncio
    async def test_unconsumed_advisory_stages_pending_context(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        await deliver_advisory(state, slot, make_note(), make_envelope())
        _requeue_unconsumed_steers(state, slot)
        staged = pop_pending_advisor_context(slot)
        assert staged, "unconsumed advisory must stage pending context"
        assert "deletes the wrong table" in staged[0]
        # Pop is destructive: staged context is delivered exactly once.
        assert pop_pending_advisor_context(slot) == []

    @pytest.mark.asyncio
    async def test_mixed_teardown_preserves_advisory_and_requeues_user(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        await deliver_advisory(state, slot, make_note(), make_envelope())
        slot._pending_steers.append("a raced user message")
        _requeue_unconsumed_steers(state, slot)
        assert len(slot._queue) == 1
        assert slot._queue[0]["content"] == "a raced user message"
        preserved = [
            m
            for m in slot.messages
            if isinstance(m.get("meta"), dict) and m["meta"].get("advisorState") == "preserved"
        ]
        assert len(preserved) == 1

    @pytest.mark.asyncio
    async def test_steer_unavailable_preserves_immediately(self, state):
        slot = _running_slot(state)
        client = MagicMock()
        client.supports_steer = False
        slot._acp_client = client
        outcome = await deliver_advisory(state, slot, make_note(), make_envelope())
        assert outcome == ADVISORY_PRESERVED
        assert slot._queue == []
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorState"] == "preserved"
        assert pop_pending_advisor_context(slot)

    @pytest.mark.asyncio
    async def test_refused_steer_preserves_and_unwinds_ledger(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=False)
        outcome = await deliver_advisory(state, slot, make_note(), make_envelope())
        assert outcome == ADVISORY_PRESERVED
        assert slot._queue == []
        assert slot._pending_steers == []
        assert slot._steer_delivery_ids == {}

    @pytest.mark.asyncio
    async def test_teardown_never_duplicates_a_preserved_advisory(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        await deliver_advisory(state, slot, make_note(), make_envelope())
        _requeue_unconsumed_steers(state, slot)
        _requeue_unconsumed_steers(state, slot)  # idempotent second teardown
        preserved = [
            m
            for m in slot.messages
            if isinstance(m.get("meta"), dict) and m["meta"].get("advisorState") == "preserved"
        ]
        assert len(preserved) == 1
        assert pop_pending_advisor_context(slot) != []
        assert pop_pending_advisor_context(slot) == []


class TestAdvisorContextDrain:
    """Round-4: staged nit/concern advice must reach the next primary turn.

    The runner drains the slot's staged context once, at turn start, into
    the outbound message -- data for the primary model to weigh, framed the
    same way live advisories are.
    """

    def test_drain_prepends_staged_context_once(self):
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_runner import (
            _commit_advisor_context_drain,
            _peek_advisor_context,
        )

        slot = SimpleNamespace(
            _advisor_pending_context=["[nit] name the constant", "[concern] check the lock order"]
        )
        out = _peek_advisor_context(slot, "user asks something")
        assert out.endswith("user asks something")
        assert "[Advisor context]" in out
        assert "name the constant" in out and "check the lock order" in out
        assert "advice, not an instruction" in out
        # peek is non-destructive; commit drains exactly once
        assert slot._advisor_pending_context == [
            "[nit] name the constant",
            "[concern] check the lock order",
        ]
        _commit_advisor_context_drain(slot)
        assert slot._advisor_pending_context == []
        assert _peek_advisor_context(slot, "next") == "next"

    def test_drain_is_inert_without_staged_context(self):
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_runner import _peek_advisor_context

        slot = SimpleNamespace(_advisor_pending_context=[])
        assert _peek_advisor_context(slot, "plain") == "plain"


class TestAdvisoryDisplayMeta:
    """UX: the transcript card renders structured fields, not the raw
    model-directed injection text. The envelope carries what the card shows;
    the injected message keeps the weigh-not-obey framing for the model."""

    def test_row_meta_carries_display_fields(self):
        env = AdvisoryEnvelope(
            severity="concern",
            advisor_update_id="k:1:2",
            reviewer_model="m",
            note_text="the script deletes the wrong tree",
            evidence="workspace/**/*.log: 0 files found",
        )
        meta = env.row_meta("preserved")
        assert meta["advisorText"] == "the script deletes the wrong tree"
        assert meta["advisorEvidence"] == "workspace/**/*.log: 0 files found"

    def test_display_fields_default_empty(self):
        env = AdvisoryEnvelope(severity="nit", advisor_update_id="k:1:3", reviewer_model="m")
        meta = env.row_meta("steered")
        assert meta["advisorText"] == ""
        assert meta["advisorEvidence"] == ""


class TestAdvisoryOutboundRedaction:
    """Round-6 GPT blocker: the reviewer's own text is model output and can
    echo a credential it read; it must pass outbound redaction before
    injection or persistence, like every other provider-bound surface."""

    def test_advisory_message_is_redacted(self):
        from kiro_crew.advisor.output import AdvisorNote

        # Assembled at runtime: the fork content scan flags the literal
        # key=value form on any added line; the runtime redactor matches the
        # assembled string identically.
        cred = "aws_secret_access" + "_key=" + "AKIA" + "IOSFODNN7EXAMPLE"
        note = AdvisorNote(
            severity="concern",
            text=f"the config leaks {cred} in plain text",
            evidence=f"saw {cred} in config.json",
        )
        message = advisory_message(note)
        assert (
            "IOSFODNN7EXAMPLE" not in message
        ), "reviewer output reached the delivery text unredacted"
        assert "[concern]" in message  # framing intact around redaction


class TestSteeredButUnconsumedPreserve:
    """Round-6 Opus: a steered advisory the turn never consumed must not grow
    a SECOND card at requeue -- the existing steered row becomes preserved,
    and the context is staged exactly once."""

    def test_preserve_after_steered_row_mutates_not_appends(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _running_slot(state)
        env = AdvisoryEnvelope(
            severity="blocker",
            advisor_update_id="k:1:1:aa",
            reviewer_model="m",
            note_text="stop",
        )
        # The steer path persisted the optimistic steered row.
        slot.append(
            "advisor",
            "[Advisor] ...\n[blocker] stop",
            "msg msg-advisor",
            meta=env.row_meta("steered"),
        )
        # Turn ends without consuming; requeue diverts to preserve.
        preserve_advisory(state, slot, "[Advisor] ...\n[blocker] stop", env)
        advisor_rows = [m for m in slot.messages if m.get("role") == "advisor"]
        assert len(advisor_rows) == 1, "duplicate card for one advisory"
        assert advisor_rows[0]["meta"]["advisorState"] == "preserved"
        assert slot._advisor_pending_context, "unconsumed advice must still stage"


class TestEnvelopeMetadataRedaction:
    """Round-7 (gpt+opus): the display fields are persisted to the transcript
    JSONL and rendered preferentially over content, so they must carry the
    SAME outbound redaction the injected message does."""

    def test_row_meta_display_fields_are_redacted(self):
        cred = "aws_secret_access" + "_key=" + "AKIA" + "IOSFODNN7EXAMPLE"
        env = AdvisoryEnvelope(
            severity="concern",
            advisor_update_id="k:1:2",
            reviewer_model="m",
            note_text=f"leaks {cred} here",
            evidence=f"saw {cred}",
        )
        meta = env.row_meta("preserved")
        assert "IOSFODNN7EXAMPLE" not in meta["advisorText"]
        assert "IOSFODNN7EXAMPLE" not in meta["advisorEvidence"]
        assert "leaks" in meta["advisorText"]  # non-secret text survives


class TestAdvisorContextDrainDeferred:
    """Round-7 gpt: the staged context must not be cleared before the turn is
    accepted, or a stop/closing exit loses the advice permanently."""

    def test_peek_does_not_clear_then_commit_does(self):
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_runner import (
            _commit_advisor_context_drain,
            _peek_advisor_context,
        )

        slot = SimpleNamespace(_advisor_pending_context=["[nit] name it"])
        out = _peek_advisor_context(slot, "user msg")
        assert "name it" in out and out.endswith("user msg")
        # peek left it intact — a pre-dispatch abort keeps the advice
        assert slot._advisor_pending_context == ["[nit] name it"]
        _commit_advisor_context_drain(slot)
        assert slot._advisor_pending_context == []
        # committing twice is safe and injects nothing new
        assert _peek_advisor_context(slot, "next") == "next"


class TestSlashTurnKeepsStagedContext:
    """A slash command streams the raw `message`, never `full_message` -- so
    its first event must not commit the advisor context drain."""

    def test_commit_gated_on_prompt_turns(self):
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        idx = src.find("_commit_advisor_context_drain(slot)")
        assert idx != -1
        gate = src[max(0, idx - 400) : idx]
        assert "not is_slash" in gate


class TestAdvisorySteerPushDiscriminator:
    """The live `steer_push` payload must carry the advisor discriminator --
    without it the client hardcodes a user bubble until page reload."""

    @pytest.mark.asyncio
    async def test_push_payload_carries_advisor_role_for_advisory(self, state):
        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        await deliver_advisory(state, slot, make_note(), make_envelope())
        pushes = [c.args for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"]
        assert pushes, "advisory steer must broadcast a steer_push"
        payload = pushes[-1][1]
        assert payload["role"] == "advisor"
        assert payload["cls"] == "msg msg-advisor"
        assert payload["advisorMeta"]["advisorSeverity"] == "blocker"
        # the raw steer flags stay out of the advisor meta copy
        assert "steer" not in payload["advisorMeta"]

    @pytest.mark.asyncio
    async def test_user_steer_push_shape_is_unchanged(self, state):
        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        await steer_into_running_turn(state, slot, "plain user steer")
        pushes = [c.args for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"]
        assert pushes
        payload = pushes[-1][1]
        assert "role" not in payload and "advisorMeta" not in payload


class TestPreserveInPlaceBroadcasts:
    """Round-10: flipping a steered row to preserved must reach live clients
    via chat_message_update, or open dashboards keep showing 'Steered'."""

    @pytest.mark.asyncio
    async def test_flip_broadcasts_chat_message_update(self, state):
        from kiro_crew.advisor.delivery import preserve_advisory

        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        env = make_envelope()
        await deliver_advisory(state, slot, make_note(), env)  # steered row
        state.broadcast_ws.reset_mock()
        preserve_advisory(state, slot, "advice text", env)  # flip in place
        patches = [
            c.args for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_message_update"
        ]
        assert patches, "in-place preserve must broadcast the row patch"
        payload = patches[-1][1]
        assert payload["meta"]["advisorState"] == "preserved"


class TestAdvisorySteerSingleDelivery:
    """Round-12: `append` broadcasts every non-user role, and steer_push also
    delivers -- the advisor row must ride ONLY steer_push or it renders twice."""

    @pytest.mark.asyncio
    async def test_steered_advisory_row_appends_without_broadcast(self, state):
        emitted = []
        slot = _running_slot(state)
        slot._on_message = lambda key, msg: emitted.append(msg)
        slot._has_reader = False
        slot._acp_client = _steer_client(accept=True)
        await deliver_advisory(state, slot, make_note(), make_envelope())
        # steer_push carries the live delivery; append must not ALSO emit the
        # row through the message callback, or clients render it twice.
        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in kinds
        assert emitted == []

    @pytest.mark.asyncio
    async def test_preserved_advisory_card_still_broadcasts(self, state):
        from kiro_crew.advisor.delivery import preserve_advisory

        emitted = []
        slot = _running_slot(state)
        slot._on_message = lambda key, msg: emitted.append(msg)
        slot._has_reader = False
        # no steer path: preservation appends the ONLY copy of the card, so
        # its append must keep broadcasting or live clients never see it.
        preserve_advisory(state, slot, "advice", make_envelope())
        assert [m for m in emitted if m.get("role") == "advisor"]


class TestHardKillDiscardsAdvice:
    """Round-12: the user's hard kill says discard EVERYTHING, reviewer advice
    included -- the unavailable-path preserve must not resurrect it."""

    @pytest.mark.asyncio
    async def test_hard_killed_advisory_is_not_preserved(self, state):
        slot = _running_slot(state)

        async def steer_suspends(message):
            # The hard kill lands while the steer RPC is suspended: it clears
            # the pending registration, the delivery id, AND the advisory
            # envelope (chat_handlers stop-force path).
            slot._pending_steers.remove(message)
            slot._steer_delivery_ids.pop(message, None)
            slot._steer_send_ids.pop(message, None)
            slot._advisory_envelopes.pop(message, None)
            return False

        client = MagicMock()
        client.supports_steer = True
        client.steer = steer_suspends
        slot._acp_client = client
        outcome = await deliver_advisory(state, slot, make_note(), make_envelope())
        from kiro_crew.advisor.delivery import ADVISORY_DISCARDED

        assert outcome == ADVISORY_DISCARDED
        # no preserved card, no staged context
        assert not [m for m in slot.messages if m.get("role") == "advisor"]
        assert not slot._advisor_pending_context


class TestHardKillFlipsPersistedRow:
    """Round-15: a hard kill landing AFTER the steered row persisted must not
    leave the transcript claiming the advice was steered -- the row flips to
    discarded and live clients get the patch."""

    def test_discard_advisory_rows_flips_state_and_broadcasts(self, state=None):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.chat_handlers import _discard_advisory_steer_rows

        st = MagicMock()
        env = SimpleNamespace(advisor_update_id="adv-77")
        row = {
            "role": "advisor",
            "content": "advice",
            "ts": "t1",
            "meta": {"advisorUpdateId": "adv-77", "advisorState": "steered", "mid": "m1"},
        }
        slot = SimpleNamespace(
            key="s1",
            messages=[row],
            _advisory_envelopes={"msg": env},
            update_message=MagicMock(return_value=row),
        )
        _discard_advisory_steer_rows(st, slot, "msg")
        assert row["meta"]["advisorState"] == "discarded"
        patches = [c for c in st.broadcast_ws.call_args_list if c.args[0] == "chat_message_update"]
        assert patches and patches[-1].args[1]["meta"]["advisorState"] == "discarded"


class TestReconciliationMarksDirty:
    """Round-16: in-place advisorState mutations persist only via the dirty
    flush -- a clean slot at restart would resurrect the pre-mutation state."""

    @pytest.mark.asyncio
    async def test_preserve_in_place_sets_dirty(self, state):
        from kiro_crew.advisor.delivery import preserve_advisory

        slot = _running_slot(state)
        slot._acp_client = _steer_client(accept=True)
        env = make_envelope()
        await deliver_advisory(state, slot, make_note(), env)
        slot._dirty = False
        preserve_advisory(state, slot, "advice", env)
        assert slot._dirty is True

    def test_discard_flip_sets_dirty(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.chat_handlers import _discard_advisory_steer_rows

        env = SimpleNamespace(advisor_update_id="adv-88")
        row = {
            "role": "advisor",
            "ts": "t1",
            "meta": {"advisorUpdateId": "adv-88", "advisorState": "steered"},
        }
        slot = SimpleNamespace(
            key="s1", messages=[row], _advisory_envelopes={"m": env}, _dirty=False
        )
        _discard_advisory_steer_rows(MagicMock(), slot, "m")
        assert slot._dirty is True
