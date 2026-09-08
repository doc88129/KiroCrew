#!/usr/bin/env python3
"""Check that a security fix killed the finding and broke nothing legitimate.

A fix that makes the proof of concept stop reproducing has done half a job. The
other half is the half a security change actually gets rejected for: the deny
fence grew a rule that also refuses ``gh pr view --json``, or a path guard now
rejects the operator's own worktree, and the tool the fix protected became one
nobody can use. Two failure modes -- **the tool became unusable** and **platform
lock-in** -- and the ``golden_paths`` table is their corpus: the legitimate
operations every security fix must keep alive. Both are nameable from this
repository's own history rather than from any clause: read-only ``gh pr view
--json`` shapes have been refused by a widened rule, and a guard written against
one host's interpreter path has cost the other host's test lane.

So this is a two-step gate, and both steps must hold::

    python3 verify_fix.py [--db PATH] --finding-id N --worktree DIR \\
        [--platform auto|posix|windows] [--timeout SECONDS]

1. ``verify_finding.py`` re-runs the proof. The fix holds ONLY when that pass
   comes back ``rejected`` -- the proof does not reproduce any more.
   ``confirmed`` means the fix did not land, and anything else means the question
   was not settled.
2. Every ACTIVE ``shell`` golden path whose platform matches the host is
   re-classified against the FIXED code. None of them may be refused.

Exit codes, which are the interface::

    0   holds        -- the proof is dead and every shell golden path is permitted
    10  reproduces   -- the proof still reproduces; the fix did not land
    30  broken       -- at least one golden path is refused (the rows are
                        printed); this is the "tool became unusable" rejection
    20  unverifiable -- something this script owns could not be settled: the
                        verifier is absent, the deny fence is not readable
    2   invalid input -- a bad argument, or a worktree that is not a checkout

Precedence when several apply is ``10 > 30 > 20 > 0``, and it is not arbitrary. A
proof that still reproduces means the fix does not exist yet, so what it did to
the golden paths is not yet a question. A broken golden path outranks an
unverifiable one because it is the actionable verdict: a named row, a named
reason, something to change. And **0 is unreachable while any check this script
owns went unsettled** -- an unclassified golden path is not a permitted one, and
reporting it as one is how a fix that broke the tool ships green.

stdout is one JSON object, carrying ``broken``, ``unverifiable`` and
``needs_human`` as lists of rows, so a reviewer gets the specific operations
rather than a count.

**Nothing out of the ledger is ever executed.** That is the load-bearing rule
here, and it is what decides how each kind is treated:

``shell``
    Classified by the deny fence, never run. The claim a shell row makes is "the
    fence must not refuse this", so classification answers it exactly and running
    the command would answer a different question. The verdict is read from
    ``kiro_crew.security.is_denied``, which returns a refusal reason or ``None``.
    It is called in a CHILD process with the worktree's own ``src`` ahead of
    everything on ``PYTHONPATH``, because the point is to classify against the
    FIXED code: importing it in this process would bind whatever copy of the
    package the interpreter already loaded, which for a test runner inside the
    repository is the unfixed one. When the import fails, every shell row is
    ``unverifiable`` and the verdict is 20 -- a fence that cannot be read is not a
    fence that agreed.

``flow`` and ``cron``
    Recorded, reported, and left to a human. Neither is executed and neither
    moves the exit code, because neither is a check this script can make:

    * A ledger row is not authorization. ``ledger.py`` states plainly that its
      CLI is not an authentication boundary -- ``--approved-by`` is an unverified
      caller assertion -- so a row is untrusted text written by whatever can
      reach the database. Every other consumer only READS such a row; running one
      as argv would turn a ledger write into command execution with the
      operator's access, which is a privilege escalation the ledger's own
      docstring says it cannot gate. No containment fixes that: the escalation is
      in treating the row as permission, not in how the child is confined.
    * A schedule is worse than unhelpful to execute -- firing one has effects
      outside the worktree that no deadline bounds -- and parsing it here would
      settle nothing either, because a parse in THIS process never consults the
      fixed code, so its answer is a constant no fix can change.

    Their value is the corpus, not a verdict: they name the operations
    (``monitor_start``, a chat turn, a shipped cron) a human has to exercise, and
    they are printed for exactly that. The shipped corpus's own well-formedness is
    asserted in the test suite, at review time, where a corpus-authoring mistake
    belongs.

Trust boundary. The target checkout is the operator's own code, which they chose
to run as themselves; containment for the verifier child is that disposable
checkout plus the deadline, not a sandbox. What this script adds on top is the
rule above: the ledger is data, never an instruction.

Reads one SQLite file through ``ledger.py`` and runs two kinds of child process --
the sibling verifier and its own classifier probe. No network of its own.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HOLDS = "holds"
REPRODUCES = "reproduces"
BROKEN = "broken"
UNVERIFIABLE = "unverifiable"

EXIT_CODES = {HOLDS: 0, REPRODUCES: 10, UNVERIFIABLE: 20, BROKEN: 30}
EXIT_INVALID = 2

#: Strongest verdict first. :func:`fold_verdict` walks this, so the precedence
#: documented above lives in ONE place and cannot drift from a chain of ``if``
#: statements that happen to be written in some order.
VERDICT_PRECEDENCE = (REPRODUCES, BROKEN, UNVERIFIABLE, HOLDS)

#: The kind whose claim this script can settle by itself. Every other kind is
#: corpus for a human, so this is the whole of what the exit code covers.
CHECKED_KIND = "shell"

DEFAULT_TIMEOUT = 120
#: How long to wait for a killed child to be reaped. Bounded for the reason
#: ``verify_finding.py`` bounds its own: the verdict is already decided, so the
#: only thing at stake is a leftover process, and blocking the conductor forever
#: on an unkillable child is worse than that.
REAP_SECONDS = 5

#: ``verify_finding.py``'s exit codes, which are its interface. Mapped rather than
#: re-derived: this script reads that contract and must not grow a second opinion
#: about what ``confirmed`` means.
VERIFIER_CONFIRMED = 0
VERIFIER_REJECTED = 10
VERIFIER_NEEDS_HUMAN = 20
VERIFIER_INVALID = 2


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path: str, data: Any, *, _mode: int = 0o666) -> None:
        return None


def script_dir() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__)))


def load_ledger() -> Any:
    """Load the sibling ledger without cwd, sys.path, or bytecode side effects.

    Mirrors ``verify_finding.py``: a skill's scripts are synced out of the package
    tree and run as bare files, so ``ledger`` is a file beside this one rather
    than an importable module, and importing it the ordinary way would drop a
    ``__pycache__`` entry into the checked-out tree.
    """
    path = str(script_dir() / "ledger.py")
    name = "_security_conductor_ledger_for_fix"
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import security conductor ledger: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def resolve_platform(requested: str) -> str:
    """The concrete host name a golden path's ``platform`` column is matched to.

    Never returns ``any``: ``any`` is a property of a ROW (it applies everywhere),
    not a host a check could run on, so accepting it as the filter would select
    the ``any`` rows and silently drop every platform-specific one.
    """
    if requested == "auto":
        return "windows" if os.name == "nt" else "posix"
    return requested


def is_git_worktree(directory: Path) -> bool:
    """Is this a git checkout -- a clone (``.git/`` directory) or a linked worktree?

    The same screen ``verify_finding.py`` applies, and for the same reason: "in a
    scratch checkout" is the whole blast-radius bound for the proof this script
    is about to re-run, and a directory nobody has established is one is not it.
    Screened HERE too so the answer is a named exit 2 rather than a 20 relayed out
    of the child.

    A linked worktree carries a ``.git`` FILE holding a gitdir pointer rather than
    a directory, so an ``is_dir()`` check would reject exactly the layout the
    conductor's brief asks for.
    """
    if not directory.is_dir():
        return False
    marker = directory / ".git"
    return marker.is_dir() or marker.is_file()


def child_env(worktree: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The closed environment a child runs in.

    Inherits nothing but ``PATH`` (a child needs an interpreter), the locale, and
    the names a Windows process needs to start at all. ``HOME`` and every scratch
    directory spelling point AT the worktree, so a ``~``-relative path or a tool's
    cache lands inside the throwaway checkout instead of the operator's real home.
    Kept deliberately identical in shape to ``verify_finding.py``'s, because "the
    same containment as the proof" is the promise, and two almost-equal allowlists
    would be two things to keep aligned.
    """
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(worktree),
        "TMPDIR": str(worktree),
        # Windows spells the scratch directory ``TEMP``/``TMP``, and that is what
        # ``tempfile`` reads there, so pinning ``TMPDIR`` alone would put a
        # child's scratch files outside the one place the blast radius is bounded.
        "TEMP": str(worktree),
        "TMP": str(worktree),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    # Three names Windows needs in order to start a process at all, and
    # withholding them hardens nothing: ``SYSTEMROOT`` is where CPython finds the
    # crypto provider it seeds ``os.urandom`` from, and ``PATHEXT``/``COMSPEC``
    # are how a bare program name resolves there. Forwarded only when the host
    # defines them, so on POSIX this loop adds nothing rather than branching.
    for name in ("SYSTEMROOT", "PATHEXT", "COMSPEC"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if extra:
        env.update(extra)
    return env


def reap(process: subprocess.Popen[bytes]) -> None:
    """Kill a child that hit the deadline, then wait once. Safe to call twice.

    The DIRECT child only, matching ``verify_finding.py`` exactly. A process-tree
    kill has no portable spelling in the standard library -- every one available is
    POSIX-only -- and a verifier whose own teardown works on a single host would be
    the PLATFORM LOCK-IN this corpus exists to catch. The two children spawned here
    are a checked-in script and this script itself, so a detached grandchild is the
    operator's own code on the operator's own machine, inside the boundary the RFC
    already draws there.
    """
    try:
        process.kill()
    except OSError:  # pragma: no cover - already reaped
        pass
    try:
        process.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_child(
    argv: list[str],
    worktree: Path,
    timeout: int,
    *,
    env_extra: dict[str, str] | None = None,
    stdin_text: str | None = None,
    capture: bool = False,
) -> tuple[str, int, str]:
    """Run one child. Returns ``(outcome, returncode, text)``.

    ``outcome`` is ``ran``, ``timeout`` or ``launch-failed``, so a caller never has
    to read one sentinel return code as three different things -- the mistake
    ``verify_finding.py`` documents at length for proofs.

    Output is captured ONLY when the caller needs to parse it. The verifier's
    output is discarded through ``DEVNULL`` rather than a pipe, because nothing
    reads it and a child that prints without stopping would otherwise buffer its
    whole stream in this process before any verdict is written.
    """
    pipe = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(worktree),
            env=child_env(worktree, extra=env_extra),
            stdout=pipe,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        )
    except OSError as exc:
        # A missing interpreter raises here rather than returning a status, and
        # letting it propagate would end the run outside the documented exit
        # contract with no verdict written at all.
        return "launch-failed", 0, str(exc)
    try:
        payload = None if stdin_text is None else stdin_text.encode("utf-8")
        out, _ = process.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        reap(process)
        return "timeout", 0, ""
    text = "" if not out else out.decode("utf-8", errors="replace")
    return "ran", int(process.returncode), text


# --------------------------------------------------------------------- step 1


def verifier_path() -> Path:
    return script_dir() / "verify_finding.py"


def run_verifier(
    *, db: Path, finding_id: int, worktree: Path, timeout: int
) -> tuple[str, str, int]:
    """Re-run the proof through ``verify_finding.py``. ``(verdict, reason, exit)``.

    The sibling is invoked BY PATH rather than imported, and its exit status is the
    whole contract this reads. That keeps one implementation of what ``confirmed``
    means: copying its judgement here would give the harness two verifiers that can
    disagree about the same proof, and the one a reviewer reads would be whichever
    they happened to run.

    The sibling ships in this same skill bundle, so an absent one is a BROKEN
    INSTALLATION rather than a state to accommodate. It is still ``unverifiable``
    and never a pass -- a proof that was not re-run says nothing about the fix -- and
    it is reported as one more invocation that could not run, which is what it is.

    The check is here rather than left to the spawn: the interpreter exists, so a
    nonexistent script argument makes the CHILD exit 2, which is the verifier's own
    code for "I rejected your input". Reading that as a rejected finding-id would
    turn a broken install into a lie about the caller's arguments.
    """
    script = verifier_path()
    if not script.is_file():
        return (
            UNVERIFIABLE,
            f"verify_finding.py could not be invoked: {script} is not a file."
            " It ships beside this script, so this is a broken installation;"
            " the proof was not re-run, and nothing about this fix is settled",
            0,
        )
    # ``--db`` is always passed, never left to the child's own default. The child
    # runs with ``HOME`` pointed at the worktree, and the default resolves under
    # ``HOME`` -- so omitting it made the verifier read a DIFFERENT ledger from the
    # one this script reads golden paths out of, and report "no such finding" for a
    # finding that is right there.
    argv = [
        sys.executable,
        str(script),
        "--db",
        str(db),
        "--finding-id",
        str(finding_id),
        "--worktree",
        str(worktree),
        "--timeout",
        str(timeout),
    ]
    # The verifier's own deadline bounds the proof; this outer one only bounds the
    # verifier's bookkeeping around it, so it gets room past the inner one rather
    # than racing it and reporting a timeout the proof did not cause.
    outcome, code, _ = run_child(argv, worktree, timeout + REAP_SECONDS + 30)
    if outcome != "ran":
        return UNVERIFIABLE, f"verify_finding.py did not complete ({outcome})", 0
    if code == VERIFIER_REJECTED:
        return HOLDS, "the proof does not reproduce any more", code
    if code == VERIFIER_CONFIRMED:
        return REPRODUCES, "the proof still reproduces; the fix did not land", code
    if code == VERIFIER_INVALID:
        return "invalid", "verify_finding.py rejected its input", code
    if code == VERIFIER_NEEDS_HUMAN:
        return UNVERIFIABLE, "verify_finding.py could not settle the proof", code
    return UNVERIFIABLE, f"verify_finding.py exited {code}, which is not in its contract", code


# ------------------------------------------------------- step 2: shell rows


#: This script re-entered in probe mode. The probe imports the product's fence, so
#: it is spelled as a re-entry rather than as an inline ``-c`` program: the same
#: import in a less inspectable shape, and its code would live in a string instead
#: of in the file a reviewer is already reading.
CLASSIFY_FLAG = "--classify-stdin"


def classifier_python(worktree: Path) -> str:
    """The interpreter the fence is read with: the worktree's own, when it has one.

    A checkout under review may pin dependencies the running interpreter does not
    have, and the fence has to be imported the way the fixed tree would import it.
    Falls back to this interpreter, which is correct whenever the package imports
    from source alone.
    """
    candidates = (
        worktree / ".venv" / "bin" / "python",
        worktree / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def classify_commands(
    commands: list[str], worktree: Path, timeout: int, *, override: list[str] | None
) -> tuple[bool, dict[str, str | None], str]:
    """Ask the deny fence about each command. ``(available, {cmd: reason}, note)``.

    ``reason`` is ``None`` for a command the fence permits and a refusal string for
    one it denies -- ``is_denied``'s own return shape, carried through rather than
    reduced to a boolean, because the reason is what tells a reviewer WHICH rule ate
    their golden path.

    ``available`` false means the fence could not be read at all, which every
    caller must turn into ``unverifiable``.
    """
    if not commands:
        return True, {}, ""
    argv = list(override) if override else [classifier_python(worktree), __file__, CLASSIFY_FLAG]
    payload = json.dumps({"commands": commands})
    outcome, code, text = run_child(
        argv,
        worktree,
        timeout,
        env_extra={"PYTHONPATH": str(worktree / "src")},
        stdin_text=payload,
        capture=True,
    )
    if outcome != "ran":
        return False, {}, f"the deny classifier probe did not complete ({outcome})"
    if code != 0:
        return False, {}, f"the deny classifier probe exited {code}"
    try:
        parsed = json.loads(text.strip().splitlines()[-1]) if text.strip() else {}
    except (ValueError, IndexError):
        return False, {}, "the deny classifier probe printed no JSON verdict"
    # Shape-checked before anything is read off it. The default probe always writes
    # an object last, so this is only reachable through ``--classifier-cmd``; an
    # operator pointing that at a program whose last line is a JSON list or number
    # would otherwise get an AttributeError out of a ``.get()`` here, exiting 1 --
    # outside the contract this script's exit codes ARE. An unreadable answer has a
    # verdict already, and it is 20.
    if not isinstance(parsed, dict):
        return False, {}, "the deny classifier probe printed JSON that is not an object"
    if not parsed.get("available"):
        return False, {}, str(parsed.get("error") or "the deny classifier is not importable")
    raw = parsed.get("results")
    if not isinstance(raw, list):
        return False, {}, "the deny classifier probe printed no result list"
    results: dict[str, str | None] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("command"), str):
            return False, {}, "the deny classifier probe printed a malformed result entry"
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            return False, {}, "the deny classifier probe printed a non-text refusal reason"
        results[item["command"]] = reason
    missing = [command for command in commands if command not in results]
    if missing:
        # A probe that answered about only some commands is not a fence that
        # permitted the rest. Refusing the whole batch keeps the unanswered rows out
        # of the passing set.
        return False, {}, f"the deny classifier probe skipped {len(missing)} command(s)"
    return True, results, ""


def classify_stdin(stream: Any, out: Any) -> int:
    """The probe, running inside the child: classify stdin's commands, print JSON.

    Imports the product's fence HERE, in a process whose ``PYTHONPATH`` leads the
    worktree, and reports an unavailable import as data rather than as a crash --
    the parent has a verdict for "the fence cannot be read" and none for a
    traceback.
    """
    try:
        request = json.loads(stream.read() or "{}")
    except ValueError as exc:
        json.dump({"available": False, "error": f"unreadable request: {exc}"}, out)
        out.write("\n")
        return 0
    commands = [str(item) for item in (request.get("commands") or [])]
    try:
        from kiro_crew.security import is_denied  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - any import failure is the same verdict
        json.dump({"available": False, "error": f"kiro_crew.security: {exc}"}, out)
        out.write("\n")
        return 0
    results = []
    for command in commands:
        try:
            reason = is_denied(command)
        except Exception as exc:  # noqa: BLE001 - a raising classifier is unreadable, not a pass
            json.dump({"available": False, "error": f"is_denied raised: {exc}"}, out)
            out.write("\n")
            return 0
        results.append({"command": command, "reason": None if reason is None else str(reason)})
    json.dump({"available": True, "results": results}, out)
    out.write("\n")
    return 0


# -------------------------------------------------------- step 2: the check


def describe_row(row: Any, why: str) -> dict[str, Any]:
    """One row as a reviewer needs it: what it is, and what happened to it.

    ``reason`` -- the human's own note about why the operation matters -- travels
    with every entry, because it is what tells a reviewer whether to change the fix
    or retire the row.
    """
    return {
        "id": int(row["id"]),
        "kind": str(row["kind"]),
        "surface": str(row["surface"]),
        "platform": str(row["platform"]),
        "command_or_flow": str(row["command_or_flow"]),
        "reason": str(row["reason"]),
        "why": why,
    }


def check_golden_paths(
    rows: list[Any], worktree: Path, timeout: int, *, classifier_override: list[str] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    """Classify the shell corpus; hand every other kind to a human, unexecuted.

    Returns ``(broken, unverifiable, needs_human, checked)``. The split is the
    design: the first two are verdicts about checks this script makes, and the third
    is corpus it deliberately makes no check about -- see the module docstring for
    why a ledger row is never run.

    ``checked`` counts the shell rows only, which is the set this script made a
    claim about. Reporting the whole table there would let a corpus of nothing but
    human rows describe itself as fully checked.
    """
    broken: list[dict[str, Any]] = []
    unsettled: list[dict[str, Any]] = []
    needs_human: list[dict[str, Any]] = []

    shell_rows = [row for row in rows if str(row["kind"]) == CHECKED_KIND]
    commands = sorted({str(row["command_or_flow"]) for row in shell_rows})
    available, verdicts, note = classify_commands(
        commands, worktree, timeout, override=classifier_override
    )
    for row in shell_rows:
        if not available:
            unsettled.append(describe_row(row, note))
            continue
        refusal = verdicts.get(str(row["command_or_flow"]))
        if refusal is not None:
            broken.append(describe_row(row, f"the deny fence refuses it: {refusal}"))

    for row in rows:
        kind = str(row["kind"])
        if kind == CHECKED_KIND:
            continue
        needs_human.append(
            describe_row(
                row,
                f"a {kind} golden path is never run from the ledger;"
                " a human has to exercise this operation",
            )
        )

    return broken, unsettled, needs_human, len(shell_rows)


def fold_verdict(*candidates: str) -> str:
    """The strongest verdict present, per :data:`VERDICT_PRECEDENCE`.

    One walk over a declared order rather than a chain of ``if`` statements, so the
    ladder documented in the module docstring is the ladder that runs. The property
    that matters is the last one: ``holds`` is reachable only when no stronger
    verdict is present at all.
    """
    for verdict in VERDICT_PRECEDENCE:
        if verdict in candidates:
            return verdict
    return HOLDS  # pragma: no cover - every caller passes at least one candidate


# ---------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a security fix and its golden paths")
    parser.add_argument("--db", default=None, help="ledger path (default: data home)")
    parser.add_argument("--finding-id", type=int, default=None)
    parser.add_argument("--worktree", default=None)
    parser.add_argument("--platform", default="auto", choices=("auto", "posix", "windows"))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    # The seam the tests drive the fence through, and the reason it is a flag rather
    # than an environment variable: a test that has to set the environment can leak
    # it into every later child, and a golden-path check reading a stray override
    # would report a fence nobody configured.
    parser.add_argument(
        "--classifier-cmd",
        default=None,
        help="JSON argv answering the classifier probe protocol (testing seam)",
    )
    parser.add_argument(CLASSIFY_FLAG, action="store_true", dest="classify_stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.classify_stdin:
        return classify_stdin(sys.stdin, sys.stdout)

    if args.finding_id is None or args.worktree is None:
        print("--finding-id and --worktree are both required", file=sys.stderr)
        return EXIT_INVALID
    if args.timeout <= 0:
        print("--timeout must be a positive number of seconds", file=sys.stderr)
        return EXIT_INVALID
    worktree = Path(args.worktree)
    if not worktree.is_dir():
        print(f"--worktree is not a directory: {worktree}", file=sys.stderr)
        return EXIT_INVALID
    if not is_git_worktree(worktree):
        print(
            f"--worktree is not a git checkout: {worktree};"
            " a disposable checkout is the whole blast-radius bound",
            file=sys.stderr,
        )
        return EXIT_INVALID
    override: list[str] | None = None
    if args.classifier_cmd:
        try:
            parsed = json.loads(args.classifier_cmd)
        except ValueError as exc:
            print(f"--classifier-cmd is not JSON: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if not isinstance(parsed, list) or not parsed:
            print("--classifier-cmd must be a non-empty JSON array", file=sys.stderr)
            return EXIT_INVALID
        override = [str(item) for item in parsed]

    # ONE resolved path, shared by this process and the verifier child. Resolved
    # here rather than defaulted twice, because the child's ``HOME`` is the worktree
    # and the default is ``HOME``-relative.
    ledger = load_ledger()
    db = (Path(args.db) if args.db else ledger.default_db_path()).resolve()

    poc_verdict, poc_reason, poc_exit = run_verifier(
        db=db, finding_id=args.finding_id, worktree=worktree, timeout=args.timeout
    )
    if poc_verdict == "invalid":
        print(poc_reason, file=sys.stderr)
        return EXIT_INVALID

    platform = resolve_platform(args.platform)
    conn = ledger.connect(db)
    try:
        ledger.init_schema(conn)
        rows = ledger.active_golden_paths(conn, platform=platform)
    finally:
        conn.close()

    # The golden paths are checked even when the proof still reproduces. The verdict
    # does not change -- ``reproduces`` outranks everything -- but a fix that failed
    # AND broke three legitimate operations is one round of feedback instead of two,
    # and the second round would only be reached after the first was fixed.
    broken, unsettled, needs_human, checked = check_golden_paths(
        list(rows), worktree, args.timeout, classifier_override=override
    )

    verdict = fold_verdict(
        poc_verdict,
        *([BROKEN] if broken else []),
        *([UNVERIFIABLE] if unsettled else []),
    )
    payload = {
        "finding_id": args.finding_id,
        "verdict": verdict,
        "platform": platform,
        "poc": {"verdict": poc_verdict, "reason": poc_reason, "exit": poc_exit},
        "golden_paths_checked": checked,
        "broken": broken,
        "unverifiable": unsettled,
        "needs_human": needs_human,
    }
    print(json.dumps(payload, sort_keys=True))
    for row in broken:
        print(f"broken golden path #{row['id']} ({row['kind']}): {row['why']}", file=sys.stderr)
    for row in unsettled:
        print(
            f"unverifiable golden path #{row['id']} ({row['kind']}): {row['why']}",
            file=sys.stderr,
        )
    for row in needs_human:
        print(
            f"needs a human #{row['id']} ({row['kind']}): {row['command_or_flow']}",
            file=sys.stderr,
        )
    return EXIT_CODES[verdict]


if __name__ == "__main__":
    sys.exit(main())
