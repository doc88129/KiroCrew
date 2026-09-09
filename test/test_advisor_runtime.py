"""Tests for the shared Advisor reviewer runtime."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest

from kiro_crew.advisor.runtime import AdvisorReviewerRuntime

_pid_counter = itertools.count(4000)


class FakeAcpRuntime:
    def __init__(self) -> None:
        self.pid = next(_pid_counter)
        self._alive = False
        self.kill_calls = 0

    def is_alive(self) -> bool:
        return self._alive

    async def spawn(self) -> None:
        self._alive = True

    async def kill(self, *, expected: bool = False) -> None:
        self._alive = False
        self.kill_calls += 1


class Harness:
    def __init__(self) -> None:
        self.created: list[FakeAcpRuntime] = []

    def factory(self) -> FakeAcpRuntime:
        runtime = FakeAcpRuntime()
        self.created.append(runtime)
        return runtime


def make_runtime(
    harness: Harness,
    *,
    max_concurrent: int = 2,
    idle_grace_secs: float = 0.02,
    prompt_fn=None,
) -> AdvisorReviewerRuntime:
    async def default_prompt(session, payload):
        return {"notes": []}

    return AdvisorReviewerRuntime(
        runtime_factory=harness.factory,
        max_concurrent=max_concurrent,
        idle_grace_secs=idle_grace_secs,
        prompt_fn=prompt_fn or default_prompt,
    )


class TestSharedRuntimeLifecycle:
    @pytest.mark.asyncio
    async def test_first_acquire_spawns_shared_runtime_once(self):
        harness = Harness()
        pool = make_runtime(harness)
        await pool.acquire_session("dashboard:a")
        await pool.acquire_session("dashboard:b")
        assert len(harness.created) == 1
        assert harness.created[0].is_alive() is True
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_same_parent_reuses_its_reviewer_session(self):
        harness = Harness()
        pool = make_runtime(harness)
        first = await pool.acquire_session("dashboard:a")
        second = await pool.acquire_session("dashboard:a")
        assert first is second
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_distinct_parents_get_isolated_sessions(self):
        harness = Harness()
        pool = make_runtime(harness)
        first = await pool.acquire_session("dashboard:a")
        second = await pool.acquire_session("dashboard:b")
        assert first is not second
        assert first.session_id != second.session_id
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_kills_runtime_and_clears_sessions(self):
        harness = Harness()
        pool = make_runtime(harness)
        await pool.acquire_session("dashboard:a")
        await pool.shutdown()
        assert harness.created[0].kill_calls == 1
        assert pool._sessions == {}


class TestIdleReaping:
    @pytest.mark.asyncio
    async def test_last_release_reaps_after_idle_grace(self):
        harness = Harness()
        pool = make_runtime(harness, idle_grace_secs=0.01)
        await pool.acquire_session("dashboard:a")
        await pool.release_session("dashboard:a")
        await asyncio.sleep(0.08)
        assert harness.created[0].kill_calls == 1

    @pytest.mark.asyncio
    async def test_release_with_other_sessions_live_does_not_reap(self):
        harness = Harness()
        pool = make_runtime(harness, idle_grace_secs=0.01)
        await pool.acquire_session("dashboard:a")
        await pool.acquire_session("dashboard:b")
        await pool.release_session("dashboard:a")
        await asyncio.sleep(0.08)
        assert harness.created[0].kill_calls == 0
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_reacquire_within_grace_cancels_reap(self):
        harness = Harness()
        pool = make_runtime(harness, idle_grace_secs=0.05)
        await pool.acquire_session("dashboard:a")
        await pool.release_session("dashboard:a")
        await pool.acquire_session("dashboard:a")
        await asyncio.sleep(0.12)
        assert harness.created[0].kill_calls == 0
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_acquire_after_reap_respawns(self):
        harness = Harness()
        pool = make_runtime(harness, idle_grace_secs=0.01)
        await pool.acquire_session("dashboard:a")
        await pool.release_session("dashboard:a")
        await asyncio.sleep(0.08)
        await pool.acquire_session("dashboard:a")
        assert len(harness.created) == 2
        await pool.shutdown()


class TestCrashSelfHeal:
    @pytest.mark.asyncio
    async def test_dead_runtime_is_replaced_on_next_acquire(self):
        harness = Harness()
        pool = make_runtime(harness)
        await pool.acquire_session("dashboard:a")
        harness.created[0]._alive = False
        await pool.acquire_session("dashboard:b")
        assert len(harness.created) == 2
        assert harness.created[1].is_alive() is True
        await pool.shutdown()


class TestBoundedReview:
    @pytest.mark.asyncio
    async def test_reviews_respect_concurrency_bound(self):
        harness = Harness()
        in_flight = 0
        peak = 0

        async def slow_prompt(session, payload):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return {"notes": []}

        pool = make_runtime(harness, max_concurrent=2, prompt_fn=slow_prompt)
        for key in ("a", "b", "c", "d"):
            await pool.acquire_session(f"dashboard:{key}")
        await asyncio.gather(
            *(pool.review(f"dashboard:{key}", {"seq": 1}) for key in ("a", "b", "c", "d"))
        )
        assert peak <= 2
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_review_failure_degrades_session_not_caller(self, caplog):
        harness = Harness()

        async def failing_prompt(session, payload):
            raise RuntimeError("reviewer exploded")

        pool = make_runtime(harness, prompt_fn=failing_prompt)
        await pool.acquire_session("dashboard:a")
        assert await pool.review("dashboard:a", {"seq": 1}) is None
        assert any("degrading" in record.getMessage() for record in caplog.records)
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_successful_review_returns_the_prompt_result(self):
        harness = Harness()
        pool = make_runtime(harness)
        await pool.acquire_session("dashboard:a")
        assert await pool.review("dashboard:a", {"seq": 1}) == {"notes": []}
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_review_for_unknown_parent_degrades_to_none(self):
        harness = Harness()
        pool = make_runtime(harness)
        assert await pool.review("dashboard:ghost", {"seq": 1}) is None


class TestReviewerAgentSpec:
    def test_packaged_agent_is_toolless_and_has_no_hooks(self):
        import kiro_crew.advisor as advisor_pkg

        path = Path(advisor_pkg.__file__).parent / "agents" / "kirocrew-advisor.json"
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert spec["tools"] == []
        assert "hooks" not in spec
        assert "allowedTools" not in spec
        assert spec["includeMcpJson"] is False


class TestReviewRuntimeHandoff:
    @pytest.mark.asyncio
    async def test_review_injects_the_live_runtime_without_mutating_payload(self):
        harness = Harness()
        seen = {}

        async def capture(session, payload):
            seen.update(payload)
            return {"notes": []}

        pool = make_runtime(harness, prompt_fn=capture)
        await pool.acquire_session("dashboard:a")
        original = {"prompt": "x", "seq": 1}
        await pool.review("dashboard:a", original)
        assert seen["_runtime"] is harness.created[0]
        assert "_runtime" not in original
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_live_predicate_reaches_the_prompt_function(self):
        harness = Harness()
        seen = {}

        async def capture(session, payload):
            seen["authorized"] = payload.get("_authorized")
            return {"notes": []}

        pool = make_runtime(harness, prompt_fn=capture)
        await pool.acquire_session("dashboard:a")
        predicate = lambda: True  # noqa: E731
        await pool.review("dashboard:a", {"prompt": "x", "_authorized": predicate})
        assert seen["authorized"] is predicate
        await pool.shutdown()


class TestPreSpawnHook:
    @pytest.mark.asyncio
    async def test_pre_spawn_runs_before_factory(self):
        harness = Harness()
        order = []

        async def pre_spawn():
            order.append("pre_spawn")

        def factory():
            order.append("factory")
            return harness.factory()

        async def prompt(session, payload):
            return {"notes": []}

        pool = AdvisorReviewerRuntime(
            runtime_factory=factory,
            prompt_fn=prompt,
            pre_spawn=pre_spawn,
        )
        await pool.acquire_session("dashboard:p")
        assert order == ["pre_spawn", "factory"]
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_pre_spawn_failure_aborts_acquisition(self):
        harness = Harness()

        async def pre_spawn():
            raise RuntimeError("spec hardening could not persist")

        async def prompt(session, payload):
            return {"notes": []}

        pool = AdvisorReviewerRuntime(
            runtime_factory=harness.factory,
            prompt_fn=prompt,
            pre_spawn=pre_spawn,
        )
        with pytest.raises(RuntimeError):
            await pool.acquire_session("dashboard:q")
        assert harness.created == []


class TestAuthorizationRevalidatedBeforeTransmission:
    @pytest.mark.asyncio
    async def test_revoked_authorization_cancels_transmission(self):
        harness = Harness()
        calls = []
        first_running = asyncio.Event()
        release_first = asyncio.Event()

        async def prompt(session, payload):
            calls.append(payload["seq"])
            if payload["seq"] == 1:
                first_running.set()
                await release_first.wait()
            return {"notes": []}

        pool = make_runtime(harness, max_concurrent=1, prompt_fn=prompt)
        await pool.acquire_session("dashboard:a")
        await pool.acquire_session("dashboard:b")
        authorized = {"b": True}
        first = asyncio.create_task(pool.review("dashboard:a", {"seq": 1}))
        await first_running.wait()
        second = asyncio.create_task(
            pool.review(
                "dashboard:b",
                {"seq": 2, "_authorized": lambda: authorized["b"]},
            )
        )
        await asyncio.sleep(0)
        authorized["b"] = False
        release_first.set()
        _, second_result = await asyncio.gather(first, second)
        assert second_result is None
        assert calls == [1]
        await pool.shutdown()


class TestRevocationCancelsTheActiveTurn:
    @pytest.mark.asyncio
    async def test_release_mid_prompt_cancels_it_and_review_degrades(self):
        harness = Harness()
        running = asyncio.Event()
        outcome = {}

        async def prompt(session, payload):
            running.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                outcome["cancelled"] = True
                raise

        pool = make_runtime(harness, prompt_fn=prompt)
        await pool.acquire_session("dashboard:a")
        task = asyncio.create_task(pool.review("dashboard:a", {"seq": 1}))
        await running.wait()
        await pool.release_session("dashboard:a")
        assert await asyncio.wait_for(task, 1) is None
        assert outcome == {"cancelled": True}
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_cancel_active_keeps_the_session(self):
        harness = Harness()
        running = asyncio.Event()

        async def prompt(session, payload):
            running.set()
            await asyncio.sleep(30)

        pool = make_runtime(harness, prompt_fn=prompt)
        await pool.acquire_session("dashboard:a")
        task = asyncio.create_task(pool.review("dashboard:a", {"seq": 1}))
        await running.wait()
        pool.cancel_active("dashboard:a")
        assert await asyncio.wait_for(task, 1) is None
        assert "dashboard:a" in pool._sessions
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_cancelled_caller_sees_its_own_cancellation(self):
        harness = Harness()
        running = asyncio.Event()

        async def prompt(session, payload):
            running.set()
            await asyncio.sleep(30)

        pool = make_runtime(harness, prompt_fn=prompt)
        await pool.acquire_session("dashboard:a")
        task = asyncio.create_task(pool.review("dashboard:a", {"seq": 1}))
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await pool.shutdown()


class TestRevocationCancelsEveryOverlappingReview:
    @pytest.mark.asyncio
    async def test_release_cancels_both_in_flight_prompts(self):
        harness = Harness()
        started = 0
        both = asyncio.Event()
        cancelled = []

        async def prompt(session, payload):
            nonlocal started
            started += 1
            if started == 2:
                both.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(payload["seq"])
                raise

        pool = make_runtime(harness, max_concurrent=2, prompt_fn=prompt)
        await pool.acquire_session("dashboard:a")
        first = asyncio.create_task(pool.review("dashboard:a", {"seq": 1}))
        second = asyncio.create_task(pool.review("dashboard:a", {"seq": 2}))
        await both.wait()
        await pool.release_session("dashboard:a")
        assert await asyncio.wait_for(asyncio.gather(first, second), 1) == [None, None]
        assert sorted(cancelled) == [1, 2]
        await pool.shutdown()


class TestStaleRuntimeIsDisplaced:
    @pytest.mark.asyncio
    async def test_stale_runtime_is_replaced_before_the_next_prompt(self):
        class StaleAware(FakeAcpRuntime):
            stale: str | None = None

            async def _is_stale(self):
                return self.stale

        class StaleHarness(Harness):
            def factory(self):
                runtime = StaleAware()
                self.created.append(runtime)
                return runtime

        harness = StaleHarness()
        pool = make_runtime(harness)
        await pool.acquire_session("dashboard:a")
        assert await pool.review("dashboard:a", {"seq": 1}) == {"notes": []}
        first = harness.created[0]
        first.stale = "rss"
        assert await pool.review("dashboard:a", {"seq": 2}) == {"notes": []}
        assert len(harness.created) == 2
        assert first.kill_calls == 1
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_stale_runtime_is_kept_while_a_prompt_is_in_flight(self):
        class StaleAware(FakeAcpRuntime):
            stale: str | None = None

            async def _is_stale(self):
                return self.stale

        class StaleHarness(Harness):
            def factory(self):
                runtime = StaleAware()
                self.created.append(runtime)
                return runtime

        running = asyncio.Event()
        release = asyncio.Event()
        harness = StaleHarness()

        async def prompt(session, payload):
            harness.created[0].stale = "age"
            running.set()
            await release.wait()
            return {"notes": []}

        pool = make_runtime(harness, max_concurrent=2, prompt_fn=prompt)
        await pool.acquire_session("dashboard:a")
        await pool.acquire_session("dashboard:b")
        first = asyncio.create_task(pool.review("dashboard:a", {"seq": 1}))
        await running.wait()
        second = asyncio.create_task(pool.review("dashboard:b", {"seq": 2}))
        await asyncio.sleep(0.05)
        assert len(harness.created) == 1
        release.set()
        await asyncio.gather(first, second)
        await pool.shutdown()
