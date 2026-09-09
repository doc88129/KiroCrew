"""Advisor composition: reviewer results into guarded, severity-routed delivery.

``AdvisorDispatcher`` is the policy seam between a reviewer's raw output and
the parent session: validate the envelope (malformed output degrades, never
injects), admit each note through the emission guard, then route by severity
-- a ``blocker`` goes through advisory delivery (steer or preserve), while
``nit`` and ``concern`` become preserved Advisor cards plus pending context
and never steer.

``build_reviewer_runtime`` wires the real collaborators for the shared
reviewer pool: an ACP runtime running the packaged read-only
``kirocrew-advisor`` agent, orphan-sweep PID protection, and a prompt
function that feeds observation updates and parses the reviewer's envelope.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from kiro_crew.advisor.delivery import (
    ADVISORY_DISCARDED,
    ADVISORY_STEERED,
    AdvisoryEnvelope,
    deliver_advisory,
    preserve_advisory,
)
from kiro_crew.advisor.guard import EmissionGuard
from kiro_crew.advisor.output import (
    AdvisorNote,
    MalformedReviewerOutput,
    parse_reviewer_envelope,
)

# Module scope per top-level-imports: `kiro_crew.agent` imports nothing from
# the advisor package, so there is no cycle to except around.
from kiro_crew.agent import (
    _apply_allowed_tools_ceiling,
    _atomic_json_write,
    kiro_agents_dir_path,
)

logger = logging.getLogger(__name__)

ADVISOR_AGENT_NAME = "kirocrew-advisor"


def ensure_advisor_agent_installed(agents_dir: Path | None = None) -> Path:
    """Materialize the packaged reviewer agent spec into the agents dir.

    The reviewer runtime spawns with ``agent=ADVISOR_AGENT_NAME``, which only
    resolves once the packaged JSON exists in the kiro agents directory.
    Copies when absent; a user's edits to an existing file are preserved.
    Either way the spec's ``allowedTools`` is re-filtered through the
    governance ceiling ON EVERY LAUNCH: the file is a persistent on-disk
    artifact, so a ceiling-denied grant left in it (an old policy, a hand
    edit) would otherwise bypass the PreToolUse gate for as long as the file
    exists. Idempotent and cheap.
    """
    if agents_dir is None:
        agents_dir = kiro_agents_dir_path()
    agents_dir = Path(agents_dir)
    target = agents_dir / f"{ADVISOR_AGENT_NAME}.json"
    if not target.exists():
        packaged = Path(__file__).parent / "agents" / f"{ADVISOR_AGENT_NAME}.json"
        agents_dir.mkdir(parents=True, exist_ok=True)
        # Atomic (tmp+rename): kiro-cli reads this spec at spawn, and a
        # truncated file from a killed install crashes every later launch.
        _atomic_json_write(target, json.loads(packaged.read_text()))
    _ceiling_filter_spec_file(target)
    return target


def _ceiling_filter_spec_file(target: Path) -> None:
    """Withhold ceiling-denied ``allowedTools`` grants from a spec file.

    Reuses the platform's one ceiling filter (`_apply_allowed_tools_ceiling`),
    so the advisor spec obeys exactly the policy every other agent spec does,
    SEL audit record included. Total: unreadable or junk JSON leaves the file
    untouched -- the runtime's own spec validation owns that failure.
    """
    try:
        spec = json.loads(target.read_text())
        if not isinstance(spec, dict) or not isinstance(spec.get("allowedTools"), list):
            return
        before = list(spec["allowedTools"])
        _apply_allowed_tools_ceiling(spec, source="advisor.ensure_agent_installed")
        if spec["allowedTools"] != before:
            _atomic_json_write(target, spec)
    except Exception:  # the advisor must never break on a malformed spec file
        logger.debug("advisor spec ceiling filter skipped", exc_info=True)


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_envelope(text: object) -> dict[str, Any]:
    """The reviewer's JSON envelope from its final text.

    Tolerates the shapes a well-behaved model actually produces -- bare JSON,
    a fenced ```json block, or one JSON object embedded in prose -- and
    raises the typed malformed error for everything else. Extraction is
    shape-tolerant; VALIDATION stays strict in ``parse_reviewer_envelope``.
    """
    if not isinstance(text, str) or not text.strip():
        raise MalformedReviewerOutput("reviewer returned no text")
    candidates = [text.strip()]
    fenced = _FENCED_JSON.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1))
    brace = text.find("{")
    if brace != -1:
        candidates.append(text[brace : text.rfind("}") + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise MalformedReviewerOutput("no JSON envelope in reviewer output")


def render_update_prompt(update: Any) -> str:
    """One observation update as the reviewer's next prompt."""
    phase = (
        f"FINAL update (turn completed, stop reason: {update.stop_reason or 'unknown'})"
        if not update.in_progress
        else "in progress"
    )
    lines = [
        f"[Session update seq={update.seq} epoch={update.epoch} "
        f"turn={update.turn_id or '?'} -- {phase}]",
    ]
    for segment in update.segments:
        lines.append(f"Assistant said:\n{segment}")
    for record in update.tool_results:
        suffix = " (truncated)" if record.truncated else ""
        lines.append(f"Tool {record.tool_name} returned{suffix}:\n{record.payload}")
    for thought in getattr(update, "reasoning", ()):
        lines.append(f"Assistant reasoning (redacted, advisory):\n{thought}")
    lines.append(
        "Review the work so far. Respond ONLY with the JSON envelope "
        '{"version": 1, "notes": [...]}; an empty notes list means the work '
        "is sound."
    )
    return "\n\n".join(lines)


class AdvisorDispatcher:
    """Routes validated, guard-admitted reviewer notes to the parent."""

    def __init__(self, guard: EmissionGuard, reviewer_model: str) -> None:
        self._guard = guard
        self._reviewer_model = reviewer_model

    async def dispatch(
        self,
        state: Any,
        slot: Any,
        raw_result: object,
        *,
        advisor_update_id: str,
        steer_allowed: bool = True,
    ) -> dict[str, int]:
        """Deliver one reviewer result; return outcome counts.

        Outcome keys: ``steered``, ``preserved``, ``degraded``. An empty dict
        means nothing needed delivery (no notes, or all suppressed).
        """
        self._guard.begin_update()
        try:
            notes = parse_reviewer_envelope(raw_result)
        except MalformedReviewerOutput:
            logger.warning(
                "advisor reviewer returned malformed output for slot %s; degrading",
                getattr(slot, "key", "?"),
            )
            return {"degraded": 1}
        logger.info(
            "advisor reviewer returned %d note(s) for slot %s",
            len(notes),
            getattr(slot, "key", "?"),
        )
        outcomes: dict[str, int] = {}
        for note in notes:
            admitted = self._guard.admit(note)
            if admitted is None:
                continue
            outcome = await self._deliver(
                state, slot, admitted, advisor_update_id, steer_allowed=steer_allowed
            )
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return outcomes

    async def _deliver(
        self,
        state: Any,
        slot: Any,
        note: AdvisorNote,
        advisor_update_id: str,
        *,
        steer_allowed: bool = True,
    ) -> str:
        envelope = AdvisoryEnvelope(
            severity=note.severity,
            advisor_update_id=f"{advisor_update_id}:{uuid.uuid4().hex[:8]}",
            reviewer_model=self._reviewer_model,
            note_text=note.text,
            evidence=note.evidence or "",
        )
        if note.severity == "blocker" and steer_allowed:
            outcome = await deliver_advisory(state, slot, note, envelope)
            if outcome == ADVISORY_STEERED:
                self._guard.note_interruption()
                return "steered"
            if outcome == ADVISORY_DISCARDED:
                return "discarded"
            return "preserved"
        # nit / concern: visible card + staged context, never a steer.
        from kiro_crew.advisor.delivery import advisory_message

        preserve_advisory(state, slot, advisory_message(note), envelope)
        return "preserved"


def build_reviewer_runtime(reviewer_model: str, work_dir: str | None = None):
    """The real reviewer pool: ACP runtime + PID protection + envelope prompt.

    Mirrors the established shared-runtime precedent: one process for every
    enabled parent, spawned with the packaged read-only reviewer agent under
    the auto sandbox. Import-light so the advisor package stays loadable
    without a live ACP stack; failures surface at first acquire, where the
    pool degrades the session visibly.
    """
    from kiro_crew.advisor.runtime import AdvisorReviewerRuntime, ReviewerSession
    from kiro_crew.agent_sdk.oneshot import (
        AgentRuntimeHandle,
        protect_runtime_pid,
        unprotect_runtime_pid,
    )

    # The runtime spawns by agent NAME; make sure the packaged spec resolves.
    try:
        ensure_advisor_agent_installed()
    except Exception:
        logger.warning("could not materialize the advisor agent spec", exc_info=True)

    def runtime_factory() -> AgentRuntimeHandle:
        # Late import so tests can monkeypatch the SDK surface.
        from kiro_crew.agent_sdk import oneshot

        return oneshot.create_agent_runtime(
            agent=ADVISOR_AGENT_NAME,
            work_dir=work_dir,
            model=reviewer_model or None,
        )

    async def prompt_fn(session: ReviewerSession, payload: dict[str, Any]) -> object:
        """Feed one observation update; return the reviewer's final text.

        Delegates the session lifecycle to the SDK's one-shot surface -- the
        dispatcher extracts and validates the envelope from the raw text.
        Read-only agent: the packaged spec needs no permissions for
        fs_read/grep/glob, so nothing on this path grants any.
        """
        from kiro_crew.agent_sdk import oneshot

        runtime = payload.pop("_runtime")
        # Evidence checks (fs_read/grep/glob) must read the OBSERVED slot's
        # tree: the shared runtime serves parents in different workspaces,
        # so each session's cwd comes from the payload, not the pool.
        cwd = payload.get("work_dir") or work_dir
        reply = await oneshot.prompt_for_reply(runtime, cwd=cwd, prompt=payload["prompt"])
        # Reviewer spend is real model spend: persist an attributed usage row
        # keyed by the reviewer's synthetic session, linked to the parent.
        # Best-effort -- accounting must never break the review itself.
        if reply.terminal is not None:
            try:
                from kiro_crew.advisor.usage import advisor_usage_kwargs
                from kiro_crew.dashboard.handlers import usage as usage_handlers

                await usage_handlers.persist_token_record_async(
                    # The SERVED model: with an empty configured reviewer model
                    # (= inherit) the backend resolves a concrete id, and spend
                    # must be attributed to what actually ran.
                    model=reply.model or reviewer_model,
                    event=reply.terminal,
                    **advisor_usage_kwargs(
                        session,
                        advisor_update_id=str(payload.get("advisor_update_id", "")),
                    ),
                )
            except Exception:
                logger.warning("advisor usage persistence failed", exc_info=True)
        return reply.text

    return AdvisorReviewerRuntime(
        runtime_factory=runtime_factory,
        protect_pid=protect_runtime_pid,
        unprotect_pid=unprotect_runtime_pid,
        prompt_fn=prompt_fn,
    )
