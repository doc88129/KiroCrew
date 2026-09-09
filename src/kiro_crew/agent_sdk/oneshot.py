"""One-shot agent prompts for advisory consumers, behind the SDK boundary.

The advisor's reviewer needs exactly three things from the agent backend:
spawn a runtime pinned to a packaged agent, keep its subprocess safe from the
orphan sweeper, and feed it one prompt collecting the text reply. Application
code may not import ``kiro_crew.acp`` directly (see
``scripts/check_agent_sdk_boundary.py``), so this module owns those three
operations inside the exempt SDK tree — the consolidation direction the
boundary gate exists to enable.

Deliberately minimal: no session reuse, no streaming surface, no tool-call
mediation. The session is created, prompted once, and destroyed; a read-only
agent spec (fs_read/grep/glob) needs no permission handling on this path.
"""

from __future__ import annotations

import logging

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.acp.worker_pool import register_protected_pid, unregister_protected_pid

logger = logging.getLogger(__name__)

AgentRuntimeHandle = AcpRuntime
"""The runtime type the one-shot surface vends; consumers annotate with this
instead of importing the ACP layer themselves."""

__all__ = [
    "AgentRuntimeHandle",
    "OneShotReply",
    "create_agent_runtime",
    "protect_runtime_pid",
    "unprotect_runtime_pid",
    "prompt_for_reply",
    "prompt_for_text",
]


class OneShotReply:
    """One prompt's outcome: text, terminal event, and the served model.

    The terminal event carries the backend's usage accounting; ``None`` when
    the stream ended without one (crash, cancel). ``model`` is the session's
    backend-resolved served model id (``""`` when unknown) -- callers
    attributing spend must prefer it over their configured model name, which
    can be empty (= inherit the gateway default).
    """

    __slots__ = ("text", "terminal", "model")

    def __init__(self, text: str, terminal: object | None, model: str = "") -> None:
        self.text = text
        self.terminal = terminal
        self.model = model


def create_agent_runtime(
    *,
    agent: str,
    work_dir: str | None,
    sandbox_mode: str = "auto",
    model: str | None = None,
) -> AcpRuntime:
    """Spawn an agent runtime pinned to a named agent spec.

    ``model`` selects the backend model for every session the runtime
    hosts; ``None`` keeps the runtime's own default resolution.
    """
    return AcpRuntime(agent=agent, work_dir=work_dir, sandbox_mode=sandbox_mode, model=model)


def protect_runtime_pid(pid: int) -> None:
    """Shield a runtime subprocess from the orphan sweeper."""
    register_protected_pid(pid)


def unprotect_runtime_pid(pid: int) -> None:
    """Release a runtime subprocess from kill-protection."""
    unregister_protected_pid(pid)


async def prompt_for_reply(runtime: AcpRuntime, *, cwd: str | None, prompt: str) -> OneShotReply:
    """Feed one prompt to a fresh session; return text plus terminal event.

    Opens a session on the given runtime (agent inherited from the runtime
    spawn), collects text chunks to the terminal event, then destroys the
    session. The generator is closed explicitly so a break mid-stream never
    leaks the underlying request. The terminal event is returned so callers
    can account the turn's usage.
    """
    handle = await runtime.create_session(cwd=cwd, agent=None)
    try:
        parts: list[str] = []
        terminal: object | None = None
        gen = handle.prompt(prompt)
        try:
            async for ev in gen:
                kind = getattr(ev, "kind", None)
                if kind == EVENT_TEXT_CHUNK:
                    parts.append(getattr(ev, "text", "") or "")
                elif kind == EVENT_PERMISSION_REQUEST:
                    # Fail-closed: this surface runs unattended (no approval
                    # UI), so an unanswered request stalls to timeout and an
                    # auto-approve would grant an escalation no policy engine
                    # has seen. Reject inline (a stdin write, cannot deadlock
                    # the read loop) and log for the audit trail; the backend
                    # turns it into a tool error and the reply still lands.
                    rid = getattr(ev, "request_id", "")
                    logger.warning(
                        "one-shot session auto-rejected permission request id=%s "
                        "(tool: %s): no approval surface exists on this path",
                        rid,
                        getattr(ev, "tool_kind", "") or "<unknown>",
                    )
                    try:
                        await handle.reject_tool(rid)
                    except Exception:
                        logger.debug("one-shot permission reject failed", exc_info=True)
                    # A permission decision is a security event: leave the
                    # same SEL record every other rejection path emits.
                    # Best-effort -- the audit must never break the reply.
                    try:
                        from kiro_crew.sel import sel

                        sel().log_api_access(
                            caller="agent_sdk.oneshot",
                            operation="tool_permission",
                            outcome="denied",
                            source="oneshot_auto_reject",
                            resources=(
                                f"request_id={rid} "
                                f"tool={getattr(ev, 'tool_kind', '') or '<unknown>'} "
                                "reason=unattended_no_approval_surface"
                            ),
                        )
                    except Exception:
                        logger.debug("one-shot permission SEL audit failed", exc_info=True)
                elif kind == EVENT_COMPLETE:
                    terminal = ev
                    break
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                await aclose()
        served = getattr(handle, "served_model", "") or ""
        return OneShotReply("".join(parts), terminal, model=str(served))
    finally:
        destroy = getattr(handle, "destroy", None)
        if destroy is not None:
            try:
                await destroy()
            except Exception:
                logger.debug("one-shot session destroy failed", exc_info=True)


async def prompt_for_text(runtime: AcpRuntime, *, cwd: str | None, prompt: str) -> str:
    """Feed one prompt to a fresh session and return the collected text."""
    reply = await prompt_for_reply(runtime, cwd=cwd, prompt=prompt)
    return reply.text
