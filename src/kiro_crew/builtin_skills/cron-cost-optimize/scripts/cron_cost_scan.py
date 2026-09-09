#!/usr/bin/env python3
"""cron_cost_scan.py -- read-only cost audit of the cron jobs already registered.

A cron job dispatched to the model pays for its whole injected context on every
wake, whether or not the wake had anything to do. Two cheaper modes already
exist: a ``script`` or ``command`` job runs with no model turn at all, and an
agent job with ``minimal_context`` keeps the model but drops the bulk of the
injected context. Neither reaches a job that was registered before the user knew
about them, which is what this script finds.

It reads two things and writes nothing:

  * ``<home>/crons.json``           -- the registered jobs
  * ``<home>/cron-history/*.jsonl`` -- what each job actually produced per run

The history is the point. Keyword matching on a job's prompt guesses at intent;
the history says what the job really did, so "31 of the last 40 runs said the
same thing" is evidence rather than inference. A job with no history yet is
still reported, with its confidence marked low.

Verdicts, one per job:

  already-zero-token       script or command job, nothing to do
  move-to-script           deterministic work, no reasoning needed
  enable-minimal-context   still needs the model, does not need the context
  leave-as-is              genuinely reasons over its input, or unsafe to change

This script never edits a job. Applying a verdict is the user's call, made with
``cron_update`` or the dashboard Schedule page, because both changes alter what
the job can see at run time. See SKILL.md for what each one costs.

Stdlib only, Python 3.9+.

Usage:
  cron_cost_scan.py [--home PATH] [--job ID] [--min-runs N] [--history N] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

#: Runs needed before the history is treated as evidence rather than a hint.
DEFAULT_MIN_RUNS = 5

#: How many of the most recent history records to read per job.
DEFAULT_HISTORY = 40

#: Share of runs that must be no-ops before a job counts as idle.
NOOP_THRESHOLD = 0.8

# ---------------------------------------------------------------------------
# Pattern sets. Each is deliberately narrow: a false "move-to-script" costs the
# user a broken job, so every pattern here has to be a phrase that only shows up
# in work a program can settle.
# ---------------------------------------------------------------------------

#: Work a program can settle with no judgement. Deliberately narrow, and only
#: pluralized where the plural is the natural spelling: broadening this set makes
#: the scan recommend MORE script rewrites, which is the unsafe direction.
DETERMINISTIC_RE = re.compile(
    r"\b("
    r"timestamps?|unchanged|last modified|mtime|newer than|older than|expired|"
    r"disk|disk space|free space|disk usage|inodes?|"
    r"file exists|already exists|is present|is missing|"
    r"http status|status code|response code|[1-5]\d\d response|"
    r"ping|ports?|reachable|is up|is down|responding|"
    r"checksums?|hash|sha256|byte size|file size|line count|row count|"
    r"thresholds?|quota|above \d|below \d|exceeds|greater than|less than|"
    r"count of|number of files|exit code|non-zero exit"
    r")\b",
    re.IGNORECASE,
)

#: Work that needs a model. Any hit blocks a script rewrite outright.
#:
#: Verb stems take a trailing ``\w*`` on purpose. A rewrite that is wrong here
#: fails SILENTLY -- the job stays green and quietly stops doing its work -- so
#: over-matching costs a missed saving while under-matching costs a broken job.
JUDGEMENT_RE = re.compile(
    r"\b("
    r"summari[sz]\w*|summary of|review\w*|draft\w*|compose\w*|write up|write a|"
    r"decide\w*|decision|judge\w*|assess\w*|evaluat\w*|interpret\w*|explain\w*|describe\w*|"
    r"analy[sz]\w*|analysis|investigat\w*|diagnos\w*|root cause|triag\w*|"
    r"recommend\w*|suggest\w*|prioriti[sz]\w*|rank\w*|classif\w*|categori[sz]\w*|"
    r"brainstorm\w*|plan\w*|propos\w*|refactor\w*|implement\w*|fix the|repl(?:y|ies)|respond to"
    r")\b",
    re.IGNORECASE,
)

#: Context a minimal-context wake does not inject. Any hit blocks that verdict.
CONTEXT_RE = re.compile(
    r"\b("
    r"memor(?:y|ies)|remember|lessons?|preferences?|steering|knowledge base|"
    r"previous session|past session|prior session|chat history|"
    r"conversation history|my notes|project context|as we discussed"
    r")\b",
    re.IGNORECASE,
)

#: ``$skill`` inline tokens. A minimal-context wake injects no skill at all, so a
#: job that names one this way stops working. Mirrors the core's own token shape.
SKILL_TOKEN_RE = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9/_-]*)")

#: A bare mention of a skill by word, for jobs that spell it out in prose.
SKILL_WORD_RE = re.compile(r"\bskill\b", re.IGNORECASE)

#: Result text that means the run found nothing to do.
NOOP_RE = re.compile(
    r"("
    r"nothing to do|nothing new|nothing to report|nothing changed|"
    r"no new |no change|no changes|unchanged|no update|no action|"
    r"none found|no matches|no results|no failures|no errors|no issues|"
    r"all clear|all good|all healthy|clean run|looks healthy|is healthy|"
    r"up to date|already (done|handled|posted|processed|triaged)|"
    r"skipped|no-op|idle|0 found|0 new|zero new"
    r")",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """What a job's run history says about whether it does any work."""

    runs: int = 0
    failures: int = 0
    distinct_summaries: int = 0
    noop_runs: int = 0
    same_every_run: bool = False

    @property
    def noop_ratio(self) -> float:
        return (self.noop_runs / self.runs) if self.runs else 0.0

    def is_evidence(self, min_runs: int) -> bool:
        """True when there are enough runs to trust what they show."""
        return self.runs >= min_runs

    def says_idle(self, min_runs: int) -> bool:
        """True when the history shows a job that repeats itself or does nothing."""
        if not self.is_evidence(min_runs):
            return False
        return self.same_every_run or self.noop_ratio >= NOOP_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "failures": self.failures,
            "distinct_summaries": self.distinct_summaries,
            "noop_runs": self.noop_runs,
            "noop_ratio": round(self.noop_ratio, 3),
            "same_every_run": self.same_every_run,
        }


@dataclass
class Finding:
    """One job's verdict, with the numbers it was derived from."""

    job_id: str
    name: str
    mode: str
    verdict: str
    confidence: str
    reason: str
    schedule: str
    wakes_per_day: float | None
    minimal_context: bool
    persistent_session: bool
    hide_in_chat: bool
    enabled: bool
    evidence: Evidence
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    change: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "mode": self.mode,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reason": self.reason,
            "schedule": self.schedule,
            "wakes_per_day": self.wakes_per_day,
            "minimal_context": self.minimal_context,
            "persistent_session": self.persistent_session,
            "hide_in_chat": self.hide_in_chat,
            "enabled": self.enabled,
            "evidence": self.evidence.to_dict(),
            "blockers": self.blockers,
            "notes": self.notes,
            "change": self.change,
        }


# ---------------------------------------------------------------------------
# Reading state off disk. Read-only, and every failure degrades to a note.
# ---------------------------------------------------------------------------


def resolve_home(override: str | None) -> Path:
    """Resolve the Kiro Crew data home the same way the product does.

    An explicit ``--home`` wins, then ``KIROCREW_HOME``, then the default.
    """
    if override:
        return Path(override).expanduser()
    env = os.environ.get("KIROCREW_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".kiro" / "crew"


def load_jobs(home: Path) -> tuple[list[dict[str, Any]], str]:
    """Read the job records. Returns ``(records, problem)``; problem is '' when fine."""
    path = home / "crons.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], f"no cron store at {path}"
    except OSError as exc:
        return [], f"cannot read {path} ({exc})"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], f"cannot parse {path} ({exc})"
    if not isinstance(data, dict):
        return [], f"unexpected shape in {path}, expected an object"
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return [], f"no job list in {path}"
    return [j for j in jobs if isinstance(j, dict)], ""


def load_history(home: Path, job_id: str, limit: int) -> list[dict[str, Any]]:
    """Read the most recent run records for one job. Missing history is not an error."""
    path = home / "cron-history" / f"{job_id}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (FileNotFoundError, OSError):
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def normalize_summary(text: str) -> str:
    """Collapse a run summary so two runs that said the same thing compare equal.

    Digits become ``#`` because a timestamp, a count or a percentage changing is
    exactly the case that looks different while meaning the same thing.
    """
    lowered = text.strip().lower()
    lowered = re.sub(r"\d+", "#", lowered)
    return re.sub(r"\s+", " ", lowered)


def summarize_history(records: Sequence[dict[str, Any]]) -> Evidence:
    """Fold run records into the counts the verdict is derived from."""
    ev = Evidence()
    seen: set[str] = set()
    for rec in records:
        status = str(rec.get("status") or "")
        if status and status != "success":
            ev.failures += 1
            continue
        ev.runs += 1
        summary = str(rec.get("summary") or "")
        seen.add(normalize_summary(summary))
        if NOOP_RE.search(summary):
            ev.noop_runs += 1
    ev.distinct_summaries = len(seen)
    ev.same_every_run = ev.runs > 1 and ev.distinct_summaries == 1
    return ev


def describe_schedule(schedule: Any) -> tuple[str, float | None]:
    """Render a schedule and, where it is a fixed interval, its wakes per day."""
    if not isinstance(schedule, dict):
        return "unknown", None
    kind = str(schedule.get("kind") or "")
    if kind == "every":
        secs = schedule.get("every_secs")
        if isinstance(secs, (int, float)) and secs > 0:
            return f"every {int(secs)}s", round(86400.0 / float(secs), 2)
        return "every (interval unset)", None
    if kind == "cron":
        expr = str(schedule.get("cron_expr") or "").strip()
        return (f"cron {expr}" if expr else "cron (expression unset)"), None
    if kind == "at":
        return "one shot", 0.0
    return kind or "unknown", None


def job_mode(job: dict[str, Any]) -> str:
    """Which dispatch shape a job uses. Script and command cost no model tokens."""
    if str(job.get("script") or "").strip():
        return "script"
    if str(job.get("command") or "").strip():
        return "command"
    return "agent"


def script_blockers(message: str) -> list[str]:
    """Reasons this job must keep a model, so a script rewrite would break it."""
    out: list[str] = []
    hit = JUDGEMENT_RE.search(message)
    if hit:
        out.append(f"the prompt asks the model to {hit.group(0).lower()}, which code cannot do")
    if SKILL_TOKEN_RE.search(message) or SKILL_WORD_RE.search(message):
        out.append("the prompt drives a skill, which only an agent turn can load")
    if CONTEXT_RE.search(message):
        out.append("the prompt reads injected context a script never receives")
    return out


def minimal_context_blockers(message: str) -> list[str]:
    """Reasons this job needs the full injected context."""
    out: list[str] = []
    if SKILL_TOKEN_RE.search(message) or SKILL_WORD_RE.search(message):
        out.append("a minimal-context wake injects no skill, and this prompt names one")
    hit = CONTEXT_RE.search(message)
    if hit:
        out.append(f"the prompt relies on {hit.group(0).lower()}, which a minimal wake drops")
    return out


def classify(job: dict[str, Any], evidence: Evidence, min_runs: int) -> Finding:
    """Decide one job's verdict from its record and its run history."""
    message = str(job.get("message") or "")
    mode = job_mode(job)
    schedule, per_day = describe_schedule(job.get("schedule"))
    minimal = bool(job.get("minimal_context"))
    persistent = bool(job.get("persistent_session"))
    hidden = bool(job.get("hide_in_chat"))
    enabled = not bool(job.get("user_paused")) and not bool(job.get("auto_paused"))

    finding = Finding(
        job_id=str(job.get("id") or ""),
        name=str(job.get("name") or ""),
        mode=mode,
        verdict="leave-as-is",
        confidence="high",
        reason="",
        schedule=schedule,
        wakes_per_day=per_day,
        minimal_context=minimal,
        persistent_session=persistent,
        hide_in_chat=hidden,
        enabled=enabled,
        evidence=evidence,
    )

    if mode in ("script", "command"):
        finding.verdict = "already-zero-token"
        finding.reason = f"a {mode} job runs with no model turn, so its wakes are already free"
        return finding

    blocked = script_blockers(message)
    finding.blockers = list(blocked)
    deterministic = bool(DETERMINISTIC_RE.search(message))
    idle = evidence.says_idle(min_runs)

    if not blocked and (deterministic or idle):
        finding.verdict = "move-to-script"
        finding.change = "set script to a file under the crons directory, and clear the prompt"
        if deterministic and idle:
            finding.confidence = "high"
            finding.reason = (
                f"the work is a mechanical check, and {_idle_phrase(evidence)} "
                "so no wake has needed reasoning"
            )
        elif idle:
            finding.confidence = "medium"
            finding.reason = (
                f"the wording is not obviously mechanical, but {_idle_phrase(evidence)} "
                "so the history says nothing is being reasoned about"
            )
        else:
            finding.confidence = "low"
            finding.reason = (
                "the work reads as a mechanical check, but there is not enough run "
                "history yet to confirm it"
            )
        if persistent:
            finding.notes.append(
                "this job currently carries its previous result into the next prompt for "
                "dedup. A script receives no such carry, so any dedup has to be rewritten "
                "as state the script itself writes and reads."
            )
        return finding

    mc_blocked = minimal_context_blockers(message)
    if not minimal and not mc_blocked:
        finding.verdict = "enable-minimal-context"
        finding.change = "set minimal_context to true"
        finding.confidence = "high" if evidence.is_evidence(min_runs) else "medium"
        if blocked:
            finding.reason = (
                f"this job needs a model because {blocked[0]}, but it does not need the "
                "full injected context"
            )
        else:
            finding.reason = (
                "this job needs a model, but it does not need the full injected context"
            )
        if evidence.noop_ratio >= NOOP_THRESHOLD and not hidden:
            finding.notes.append(
                "most runs report nothing. Setting hide_in_chat to true keeps those out of "
                "the chat without changing what the job can see."
            )
        return finding

    finding.verdict = "leave-as-is"
    finding.blockers = list(dict.fromkeys(blocked + mc_blocked))
    if minimal:
        # Say this first even when a blocker exists. "Nothing to change here" is the
        # fact the user acts on; the blocker only explains why.
        if finding.blockers:
            finding.reason = f"already on minimal context, and {finding.blockers[0]}"
        else:
            finding.reason = "already on minimal context, and the work still needs a model"
    elif finding.blockers:
        finding.reason = finding.blockers[0]
    else:
        finding.reason = "already on the cheapest mode this job can safely use"
    return finding


def _idle_phrase(evidence: Evidence) -> str:
    """Say what the history showed, in runs rather than in adjectives."""
    if evidence.same_every_run:
        return f"all {evidence.runs} recorded runs produced the same result,"
    return f"{evidence.noop_runs} of {evidence.runs} recorded runs found nothing to do,"


def scan(home: Path, min_runs: int, history_limit: int, only: str | None) -> list[Finding]:
    """Classify every job in the store, or one job when ``only`` is given."""
    records, problem = load_jobs(home)
    if problem:
        raise LookupError(problem)
    findings: list[Finding] = []
    for job in records:
        job_id = str(job.get("id") or "")
        if only and only not in (job_id, str(job.get("name") or "")):
            continue
        evidence = summarize_history(load_history(home, job_id, history_limit))
        findings.append(classify(job, evidence, min_runs))
    return findings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ORDER = {
    "move-to-script": 0,
    "enable-minimal-context": 1,
    "leave-as-is": 2,
    "already-zero-token": 3,
}


def render_text(findings: Sequence[Finding], min_runs: int) -> str:
    """A report meant to be read by a person and acted on one job at a time."""
    lines: list[str] = []
    ordered = sorted(findings, key=lambda f: (_ORDER.get(f.verdict, 9), f.name))
    counts: dict[str, int] = {}
    for f in ordered:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1

    lines.append(f"Scanned {len(findings)} cron job(s).")
    for verdict in sorted(counts, key=lambda v: _ORDER.get(v, 9)):
        lines.append(f"  {counts[verdict]:>3}  {verdict}")
    lines.append("")

    for f in ordered:
        head = f"{f.name or '(unnamed)'}  [{f.job_id}]"
        if not f.enabled:
            head += "  (paused)"
        lines.append(head)
        lines.append(f"  mode        {f.mode}, {f.schedule}")
        if f.wakes_per_day is not None:
            lines.append(f"  wakes/day   {f.wakes_per_day:g}")
        ev = f.evidence
        if ev.runs or ev.failures:
            lines.append(
                f"  history     {ev.runs} run(s), {ev.distinct_summaries} distinct result(s), "
                f"{ev.noop_runs} found nothing to do, {ev.failures} failed"
            )
        else:
            lines.append("  history     none recorded yet")
        lines.append(f"  verdict     {f.verdict} ({f.confidence} confidence)")
        lines.append(f"  because     {f.reason}")
        if f.change:
            lines.append(f"  change      {f.change}")
        for blocker in f.blockers:
            lines.append(f"  blocker     {blocker}")
        for note in f.notes:
            lines.append(f"  note        {note}")
        lines.append("")

    lines.append(
        f"Run history is treated as evidence at {min_runs} or more recorded runs. "
        "Below that a verdict is marked low or medium confidence."
    )
    lines.append(
        "Kiro Crew's own in-code estimate for a minimal-context wake is roughly 200 tokens "
        "against 30,000 to 55,000 for a full one. That range is an estimate written into the "
        "product, not a measurement, so quote it as an estimate."
    )
    lines.append("Nothing was changed. Applying a verdict is a separate, explicit step.")
    return "\n".join(lines)


def render_json(findings: Sequence[Finding], min_runs: int) -> str:
    payload = {
        "min_runs": min_runs,
        "noop_threshold": NOOP_THRESHOLD,
        "scanned": len(findings),
        "findings": [f.to_dict() for f in findings],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cron_cost_scan.py",
        description="Read-only cost audit of registered cron jobs. Changes nothing.",
    )
    parser.add_argument(
        "--home", help="Kiro Crew data home. Defaults to KIROCREW_HOME or the standard location."
    )
    parser.add_argument("--job", help="Scan one job only, by id or exact name.")
    parser.add_argument(
        "--min-runs",
        type=int,
        default=DEFAULT_MIN_RUNS,
        help=f"Recorded runs needed before history counts as evidence (default {DEFAULT_MIN_RUNS}).",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=DEFAULT_HISTORY,
        help=f"How many recent runs to read per job (default {DEFAULT_HISTORY}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    if args.min_runs < 1:
        parser.error("--min-runs must be at least 1")
    if args.history < 1:
        parser.error("--history must be at least 1")

    home = resolve_home(args.home)
    try:
        findings = scan(home, args.min_runs, args.history, args.job)
    except LookupError as exc:
        print(f"cron_cost_scan: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(render_json(findings, args.min_runs))
    else:
        print(render_text(findings, args.min_runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
