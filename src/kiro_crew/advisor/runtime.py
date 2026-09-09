"""Advisor reviewer runtime: one bounded, protected, self-healing pool.

One shared reviewer runtime process serves every enabled parent session (v1
has a single global reviewer model), with one isolated reviewer session per
parent. The lifecycle mirrors the existing worker-pool precedent: serialized
startup, orphan-sweep PID protection while alive, bounded review concurrency,
replacement of a dead runtime on the next acquire, and reaping after an idle
grace once the last reviewer session is released.

Collaborators are injected (runtime factory, PID protection hooks, the
prompt function) so policy stays testable without a live ACP process; the
service layer wires the real ones.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)

#: Tools the built-in reviewer agent may call: read-only evidence gathering
#: only. Mutation, spawn, session, messaging, shell, browser, and memory
#: writes are deliberately absent; the reviewer advises, it never acts.
ADVISOR_TOOL_ALLOWLIST: tuple[str, ...] = (
    "fs_read",
    "grep",
    "glob",
)

#: Advisor session status values surfaced to the dashboard.
STATUS_WATCHING = "watching"
STATUS_REVIEWING = "reviewing"
STATUS_DEGRADED = "degraded"

_session_ids = itertools.count(1)


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
        self.session_id = f"advisor:{next(_session_ids)}"
        self.status = STATUS_WATCHING


class AdvisorReviewerRuntime:
    """Owns the shared reviewer process and per-parent reviewer sessions."""

    def __init__(
        self,
        runtime_factory: Callable[[], _RuntimeLike],
        protect_pid: Callable[[int], None],
        unprotect_pid: Callable[[int], None],
        prompt_fn: Callable[[ReviewerSession, dict[str, Any]], Awaitable[Any]],
        max_concurrent: int = 2,
        idle_grace_secs: float = 30.0,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._protect_pid = protect_pid
        self._unprotect_pid = unprotect_pid
        self._prompt_fn = prompt_fn
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._idle_grace_secs = idle_grace_secs
        self._runtime: _RuntimeLike | None = None
        self._lock = asyncio.Lock()
        self._sessions: dict[str, ReviewerSession] = {}
        self._reap_task: asyncio.Task[None] | None = None

    # -- sessions ----------------------------------------------------------

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
        """Dispose a parent's reviewer session; reap the runtime when idle."""
        async with self._lock:
            self._sessions.pop(parent_session_key, None)
            if not self._sessions and self._runtime is not None:
                self._schedule_reap()

    def session_count(self) -> int:
        return len(self._sessions)

    def status(self, parent_session_key: str) -> str | None:
        session = self._sessions.get(parent_session_key)
        return session.status if session else None

    # -- reviewing ---------------------------------------------------------

    async def review(self, parent_session_key: str, payload: dict[str, Any]) -> Any | None:
        """Run one bounded review prompt for a parent session.

        Reviewer failures never propagate: the session degrades visibly and
        the caller receives ``None``. The primary turn must not be affected
        by a broken reviewer.
        """
        session = self._sessions[parent_session_key]
        async with self._semaphore:
            session.status = STATUS_REVIEWING
            # The prompt function opens its reviewer session on the SHARED
            # runtime, which only this pool holds; hand it over on a copy so
            # the caller's payload is never mutated. Re-checked under the
            # lock so a crash between acquire and review still self-heals.
            async with self._lock:
                runtime = await self._ensure_runtime_locked()
            payload = dict(payload)
            payload["_runtime"] = runtime
            try:
                result = await self._prompt_fn(session, payload)
            except asyncio.CancelledError:
                session.status = STATUS_WATCHING
                raise
            except Exception:
                logger.warning(
                    "advisor reviewer prompt failed; degrading session",
                    exc_info=True,
                )
                session.status = STATUS_DEGRADED
                return None
            session.status = STATUS_WATCHING
            return result

    # -- lifecycle ---------------------------------------------------------

    async def shutdown(self) -> None:
        """Kill the shared runtime and clear all reviewer sessions."""
        self._cancel_reap()
        async with self._lock:
            self._sessions.clear()
            await self._kill_runtime_locked()

    async def _ensure_runtime_locked(self) -> _RuntimeLike:
        runtime = self._runtime
        if runtime is not None and not runtime.is_alive():
            # Crash self-heal: drop protection for the dead process and
            # replace it on this acquire.
            if runtime.pid is not None:
                self._unprotect_pid(runtime.pid)
            self._runtime = runtime = None
        if runtime is None:
            runtime = self._runtime_factory()
            await runtime.spawn()
            if runtime.pid is not None:
                self._protect_pid(runtime.pid)
            self._runtime = runtime
        return runtime

    async def _kill_runtime_locked(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        self._runtime = None
        try:
            await runtime.kill(expected=True)
        finally:
            if runtime.pid is not None:
                self._unprotect_pid(runtime.pid)

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
