"""Advisor reviewer runtime: one bounded, self-healing pool.

One shared reviewer runtime process serves every enabled parent session, with
one isolated reviewer session per parent. The lifecycle mirrors the existing
worker-pool precedent: serialized startup, bounded review concurrency,
replacement of a dead runtime on the next acquire, and reaping after an idle
grace once the last reviewer session is released. PID protection is the
runtime's own (``AcpRuntime``).

Collaborators are injected (runtime factory, prompt function) so policy stays
testable without a live ACP process; the service layer wires the real ones.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class _RuntimeLike(Protocol):
    """The slice of the ACP runtime surface the pool relies on."""

    @property
    def pid(self) -> int | None: ...

    def is_alive(self) -> bool: ...

    async def spawn(self) -> None: ...

    async def kill(self, *, expected: bool = False) -> None: ...


class ReviewerSession:
    """One parent's isolated reviewer conversation on the shared runtime."""

    def __init__(self, parent_session_key: str) -> None:
        self.parent_session_key = parent_session_key
        # Derived from the parent key so reviewer spend stays traceable across
        # restarts; distinct from the parent's own usage key by the prefix.
        self.session_id = f"advisor:{parent_session_key}"


class AdvisorReviewerRuntime:
    """Owns the shared reviewer process and per-parent reviewer sessions."""

    def __init__(
        self,
        runtime_factory: Callable[[], _RuntimeLike],
        prompt_fn: Callable[[ReviewerSession, dict[str, Any]], Awaitable[Any]],
        max_concurrent: int = 2,
        idle_grace_secs: float = 30.0,
        pre_spawn: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        #: Async hook awaited before every runtime spawn. The composition
        #: layer offloads agent-spec materialization here so it never blocks
        #: the event loop. Its failure aborts acquisition.
        self._pre_spawn = pre_spawn
        self._prompt_fn = prompt_fn
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._idle_grace_secs = idle_grace_secs
        self._runtime: _RuntimeLike | None = None
        self._lock = asyncio.Lock()
        self._sessions: dict[str, ReviewerSession] = {}
        self._reap_task: asyncio.Task[None] | None = None
        #: In-flight prompts per parent can overlap, so a revocation cancels
        #: every active review rather than only the latest one.
        self._active: dict[str, set[asyncio.Task[Any]]] = {}
        self._revoked: set[asyncio.Task[Any]] = set()

    async def acquire_session(self, parent_session_key: str) -> ReviewerSession:
        """Return the parent's reviewer session, creating runtime as needed."""
        self._cancel_reap()
        async with self._lock:
            await self._ensure_runtime_locked()
            session = self._sessions.get(parent_session_key)
            if session is None:
                session = ReviewerSession(parent_session_key)
                self._sessions[parent_session_key] = session
            return session

    async def release_session(self, parent_session_key: str) -> None:
        """Dispose a parent's reviewer session and reap the runtime when idle."""
        self.cancel_active(parent_session_key)
        async with self._lock:
            self._sessions.pop(parent_session_key, None)
            if not self._sessions and self._runtime is not None:
                self._schedule_reap()

    def cancel_active(self, parent_session_key: str) -> None:
        """Cancel every in-flight prompt for a parent while keeping its session."""
        for task in list(self._active.get(parent_session_key, ())):
            if not task.done():
                self._revoked.add(task)
                task.cancel()

    async def review(self, parent_session_key: str, payload: dict[str, Any]) -> Any | None:
        """Run one bounded review prompt for a parent session.

        Reviewer failures never propagate: the caller receives ``None`` and
        the primary turn continues.
        """
        session = self._sessions.get(parent_session_key)
        if session is None:
            logger.warning(
                "advisor review requested for unacquired session %s; degrading",
                parent_session_key,
            )
            return None
        authorized = payload.get("_authorized")
        async with self._semaphore:
            if callable(authorized) and not authorized():
                logger.info(
                    "advisor review cancelled before transmission: authorization "
                    "revoked while queued for %s",
                    parent_session_key,
                )
                return None
            try:
                async with self._lock:
                    runtime = await self._ensure_runtime_locked()
            except Exception:
                logger.warning(
                    "advisor reviewer runtime unavailable; degrading session",
                    exc_info=True,
                )
                return None
            prompt_payload = dict(payload)
            prompt_payload["_runtime"] = runtime
            if callable(authorized) and not authorized():
                logger.info(
                    "advisor review cancelled before transmission: authorization "
                    "revoked during runtime acquisition for %s",
                    parent_session_key,
                )
                return None
            task: asyncio.Task[Any] = asyncio.ensure_future(
                self._prompt_fn(session, prompt_payload)
            )
            self._active.setdefault(parent_session_key, set()).add(task)
            try:
                return await task
            except asyncio.CancelledError:
                if task in self._revoked:
                    logger.info(
                        "advisor review cancelled: authorization revoked mid-turn for %s",
                        parent_session_key,
                    )
                    return None
                raise
            except Exception:
                logger.warning(
                    "advisor reviewer prompt failed; degrading session",
                    exc_info=True,
                )
                return None
            finally:
                active = self._active.get(parent_session_key)
                if active is not None:
                    active.discard(task)
                    if not active:
                        self._active.pop(parent_session_key, None)
                self._revoked.discard(task)

    async def shutdown(self) -> None:
        """Kill the shared runtime and clear all reviewer sessions."""
        self._cancel_reap()
        async with self._lock:
            self._sessions.clear()
            await self._kill_runtime_locked()

    async def _ensure_runtime_locked(self) -> _RuntimeLike:
        runtime = self._runtime
        if runtime is not None and not runtime.is_alive():
            self._runtime = runtime = None
        elif runtime is not None and not self._active and await self._stale_reason(runtime):
            await self._kill_runtime_locked()
            runtime = None
        if runtime is None:
            if self._pre_spawn is not None:
                await self._pre_spawn()
            runtime = self._runtime_factory()
            await runtime.spawn()
            self._runtime = runtime
        return runtime

    @staticmethod
    async def _stale_reason(runtime: _RuntimeLike) -> str | None:
        probe = getattr(runtime, "_is_stale", None)
        if not callable(probe):
            return None
        try:
            reason = await probe()
        except Exception:
            logger.debug("advisor reviewer staleness probe failed", exc_info=True)
            return None
        if reason:
            logger.info("advisor reviewer runtime recycled: %s", reason)
        return str(reason) if reason else None

    async def _kill_runtime_locked(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        self._runtime = None
        await runtime.kill(expected=True)

    def _schedule_reap(self) -> None:
        self._cancel_reap()
        self._reap_task = asyncio.get_running_loop().create_task(self._reap_after_grace())

    def _cancel_reap(self) -> None:
        task = self._reap_task
        if task is not None and not task.done():
            task.cancel()
        self._reap_task = None

    async def _reap_after_grace(self) -> None:
        await asyncio.sleep(self._idle_grace_secs)
        async with self._lock:
            if self._sessions:
                return
            await self._kill_runtime_locked()
