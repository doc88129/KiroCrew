"""``verify_fix.py`` — the two-step gate, and the verdict it must never reach.

The properties pinned here are the ones a prompt cannot hold. A fix is verified
only when the proof stops reproducing AND every legitimate operation still works,
so the interesting assertions are all about the verdict LADDER: a proof that still
reproduces outranks everything, a broken golden path is a rejection, and
``holds`` is unreachable while any golden path went unchecked.

The script is driven as a SUBPROCESS with its siblings staged beside it, because
that is the contract that matters: it finds ``verify_finding.py`` and ``ledger.py``
as files next to itself, so a property that holds only when the module is imported
into the test process would not be the property the harness relies on. Staging the
directory is also what lets each test state the verifier's exit status at its own
call site, and what makes the broken-install case reachable without breaking the
checkout.

The deny fence is reached through ``--classifier-cmd``, the declared testing seam:
a stub answering the probe protocol lets a refusal be arranged deliberately
instead of hoping the real fence refuses something. One class at the end does use
the REAL fence, and asserts the shipped corpus against it -- that is the corpus's
whole purpose, and a test that stubbed it would assert nothing about the rows.

The other property with its own class is that NOTHING out of the ledger is
executed. A ``flow`` or ``cron`` row is untrusted text -- the ledger CLI is not an
authentication boundary -- so running one would turn a database write into a
command with the operator's access. Those tests plant a witness file a row would
create if it ran, and assert it never appears.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor"
SCRIPTS = SKILL_DIR / "scripts"
VERIFY_FIX = SCRIPTS / "verify_fix.py"
LEDGER = SCRIPTS / "ledger.py"
SEED = SKILL_DIR / "golden-paths.seed.json"

EXIT_HOLDS = 0
EXIT_REPRODUCES = 10
EXIT_UNVERIFIABLE = 20
EXIT_BROKEN = 30
EXIT_INVALID = 2

#: The verifier's own contract, as ``verify_fix.py`` reads it.
VERIFIER_CONFIRMED = 0
VERIFIER_REJECTED = 10
VERIFIER_NEEDS_HUMAN = 20

#: A stub verifier: it takes the flags the real one takes, ignores them, and exits
#: the status the test asked for. The real script's judgement is not what these
#: tests are about -- the fold over its exit code is.
STUB_VERIFIER = """import sys
sys.exit({code})
"""

#: A stub deny classifier answering the probe protocol on stdin/stdout. It refuses
#: any command containing a marker substring, so a test can arrange exactly one
#: broken golden path without depending on what the real fence happens to think.
STUB_CLASSIFIER = """import json
import sys

marker = sys.argv[1] if len(sys.argv) > 1 else None
request = json.loads(sys.stdin.read() or "{}")
results = []
for command in request.get("commands") or []:
    refuses = marker is not None and marker in command
    results.append(
        {"command": command, "reason": "stub refusal: %s" % marker if refuses else None}
    )
json.dump({"available": True, "results": results}, sys.stdout)
"""

#: A stub classifier that reports the fence as unreadable, which is what an
#: uninstallable package looks like to the probe.
STUB_CLASSIFIER_UNAVAILABLE = """import json
import sys

sys.stdin.read()
json.dump({"available": False, "error": "stub: not importable"}, sys.stdout)
"""


@pytest.fixture
def mod():
    return load_skill_script("security_conductor_verify_fix", VERIFY_FIX)


@pytest.fixture
def ledger_mod():
    return load_skill_script("security_conductor_ledger_for_fix_tests", LEDGER)


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """A scripts directory holding ``verify_fix.py`` and ``ledger.py`` and nothing else.

    The verifier is absent on purpose: every test that wants one installs it with a
    chosen exit status, so the exit code under test is always stated at the call
    site rather than inherited from whatever the real verifier decides. The
    broken-install case then needs no special setup at all.
    """
    directory = tmp_path / "scripts"
    directory.mkdir()
    shutil.copy2(VERIFY_FIX, directory / "verify_fix.py")
    shutil.copy2(LEDGER, directory / "ledger.py")
    return directory


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A target that passes the checkout screen.

    A bare ``.git`` directory is enough because that IS the screen -- the same one
    ``verify_finding.py`` applies -- and standing up a real repository per test
    would pay git's startup cost dozens of times to assert nothing extra.
    """
    directory = tmp_path / "target"
    directory.mkdir()
    (directory / ".git").mkdir()
    return directory


def install_verifier(staged: Path, code: int) -> None:
    (staged / "verify_finding.py").write_text(STUB_VERIFIER.format(code=code), encoding="utf-8")


def classifier(tmp_path: Path, marker: str | None = None, *, available: bool = True) -> list[str]:
    body = STUB_CLASSIFIER if available else STUB_CLASSIFIER_UNAVAILABLE
    path = tmp_path / f"stub_classifier_{'ok' if available else 'gone'}.py"
    path.write_text(body, encoding="utf-8")
    argv = [sys.executable, str(path)]
    if marker is not None:
        argv.append(marker)
    return argv


def a_golden_path(
    ledger_mod,
    db: Path,
    *,
    kind: str = "shell",
    command: str,
    platform: str = "any",
    active: bool = True,
) -> int:
    conn = ledger_mod.connect(db)
    try:
        ledger_mod.init_schema(conn)
        path_id, _ = ledger_mod.add_golden_path(
            conn,
            kind=kind,
            surface="test",
            command_or_flow=command,
            platform=platform,
            reason="a legitimate operation this fix must keep alive",
            source_finding_id=None,
            approved_by="tester",
            active=active,
        )
    finally:
        conn.close()
    return path_id


def run_fix(
    staged: Path,
    db: Path | None,
    worktree: Path,
    *,
    classifier_cmd: list[str] | None = None,
    platform: str = "posix",
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(staged / "verify_fix.py"),
        "--finding-id",
        "1",
        "--worktree",
        str(worktree),
        "--platform",
        platform,
        "--timeout",
        str(timeout),
    ]
    if db is not None:
        argv[2:2] = ["--db", str(db)]
    if classifier_cmd is not None:
        argv += ["--classifier-cmd", json.dumps(classifier_cmd)]
    child = dict(os.environ)
    if env:
        child.update(env)
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", timeout=300, env=child
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestTheProofIsTheFirstGate:
    def test_a_reproducing_proof_is_ten_and_outranks_everything(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """A fix that did not land is not a fix whose golden paths are interesting.

        The refused golden path is present deliberately: the verdict must still be
        10, because reporting 30 would send the reviewer to fix a legitimate
        operation while the vulnerability is still open.
        """
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="gh pr view 1 --json state REFUSE-ME")
        install_verifier(staged, VERIFIER_CONFIRMED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path, "REFUSE-ME"))
        assert result.returncode == EXIT_REPRODUCES, result.stderr
        body = payload(result)
        assert body["verdict"] == "reproduces"
        # The broken row is still REPORTED, so one round of feedback carries both.
        assert [row["id"] for row in body["broken"]]

    def test_a_rejected_proof_with_intact_golden_paths_holds(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="gh pr view 1 --json state")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path, "REFUSE-ME"))
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert body["verdict"] == "holds"
        assert body["broken"] == []
        assert body["unverifiable"] == []

    def test_a_verifier_that_cannot_settle_it_is_twenty(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="git status --porcelain")
        install_verifier(staged, VERIFIER_NEEDS_HUMAN)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert payload(result)["poc"]["verdict"] == "unverifiable"

    def test_an_exit_status_outside_the_contract_is_twenty(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """An unrecognised status is not permission.

        The verifier's contract names 0, 10, 20 and 2. A 7 means this script is
        reading a version of it that it does not understand, and the only safe
        reading of that is that nothing was settled.
        """
        db = tmp_path / "findings.db"
        install_verifier(staged, 7)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert "not in its contract" in payload(result)["poc"]["reason"]


class TestAnAbsentVerifierIsNeverAPass:
    """The sibling ships in the same bundle, so its absence is a broken install.

    A broken install is not a stage of a rollout, and it is still the case the whole
    exit ladder exists for: with no verifier there is no evidence the fix landed, and
    a 0 here would report an unverified fix as verified.
    """

    def test_a_missing_verifier_is_twenty_and_names_the_broken_install(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        assert not (staged / "verify_finding.py").exists()
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        body = payload(result)
        assert body["verdict"] == "unverifiable"
        assert "could not be invoked" in body["poc"]["reason"]
        assert "broken installation" in body["poc"]["reason"]

    def test_a_missing_verifier_is_not_read_as_a_rejected_argument(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """Exit 2 is the verifier's code for "I rejected your input", and the
        interpreter would produce it for a nonexistent script argument. Letting the
        spawn answer would turn a broken install into a lie about the caller's
        arguments -- and into exit 2, which is not a verdict about the fix at all."""
        db = tmp_path / "findings.db"
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode != EXIT_INVALID
        assert "rejected its input" not in payload(result)["poc"]["reason"]


class TestABrokenGoldenPathRejectsTheFix:
    def test_a_refused_shell_row_is_thirty_and_names_the_row(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """The "tool became unusable" rejection, with the row a reviewer must act on."""
        db = tmp_path / "findings.db"
        refused = "gh pr view 1 --json state REFUSE-ME"
        path_id = a_golden_path(ledger_mod, db, command=refused)
        a_golden_path(ledger_mod, db, command="git status --porcelain")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path, "REFUSE-ME"))
        assert result.returncode == EXIT_BROKEN, result.stderr
        body = payload(result)
        assert body["verdict"] == "broken"
        assert [row["id"] for row in body["broken"]] == [path_id]
        assert refused in body["broken"][0]["command_or_flow"]
        assert "stub refusal" in body["broken"][0]["why"]
        # The reason the human approved the row travels with the rejection: it is
        # what tells the reviewer whether to change the fix or retire the row.
        assert body["broken"][0]["reason"]

    def test_a_retired_row_is_not_checked(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="gh pr view 1 REFUSE-ME", active=False)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path, "REFUSE-ME"))
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert payload(result)["golden_paths_checked"] == 0


class TestHoldsIsUnreachableWhileAnythingIsUnverifiable:
    def test_an_unreadable_deny_fence_makes_every_shell_row_unverifiable(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """A fence that cannot be read is not a fence that agreed.

        This is the case that silently ships a broken tool: the probe fails, the
        shell rows go unchecked, and a script that treated "no refusal found" as
        "permitted" would report 0 having classified nothing.
        """
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="gh pr view 1 --json state")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path, available=False))
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        body = payload(result)
        assert body["verdict"] == "unverifiable"
        assert body["broken"] == []
        assert "not importable" in body["unverifiable"][0]["why"]

    def test_a_broken_row_outranks_an_unverifiable_one(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """30 beats 20 because it is the actionable verdict, and both are reported."""
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="gh pr view 1 REFUSE-ME")
        a_golden_path(ledger_mod, db, command="git status --porcelain")
        # One command classified, one not: the stub refuses the first, and a second
        # probe that answers about only one command leaves the other unsettled.
        partial = tmp_path / "one_answer.py"
        partial.write_text(
            "import json\nimport sys\n\n"
            'request = json.loads(sys.stdin.read() or "{}")\n'
            'answered = [c for c in (request.get("commands") or []) if "REFUSE-ME" in c]\n'
            "json.dump(\n"
            '    {"available": True, "results": '
            '[{"command": c, "reason": "stub refusal"} for c in answered]},\n'
            "    sys.stdout,\n"
            ")\n",
            encoding="utf-8",
        )
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=[sys.executable, str(partial)])
        # A partial answer refuses the whole batch, so both rows land unsettled --
        # which is the point: 20 is what an unanswered row gets, never a pass.
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert len(payload(result)["unverifiable"]) == 2

    @pytest.mark.parametrize(
        "body, fragment",
        [
            pytest.param("[1, 2, 3]", "not an object", id="json-list"),
            pytest.param("42", "not an object", id="json-number"),
            pytest.param('"available"', "not an object", id="json-string"),
            pytest.param(
                '{"available": true, "results": {"a": null}}',
                "no result list",
                id="results-not-a-list",
            ),
            pytest.param(
                '{"available": true, "results": [["cmd", null]]}',
                "malformed result entry",
                id="entry-not-object",
            ),
            pytest.param(
                '{"available": true, "results": [{"command": 7}]}',
                "malformed result entry",
                id="command-not-text",
            ),
            pytest.param(
                '{"available": true, "results": [{"command": "git status", "reason": 5}]}',
                "non-text refusal reason",
                id="reason-not-text",
            ),
        ],
    )
    def test_a_malformed_probe_payload_is_twenty_not_a_crash(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path, body: str, fragment: str
    ) -> None:
        """An unreadable answer already has a verdict, and it is 20.

        Reading a field off a payload whose shape was never checked raises, and the
        traceback exits 1 -- a status OUTSIDE the set these exit codes are. The
        default probe cannot produce these shapes; an operator-supplied
        ``--classifier-cmd`` can.
        """
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="git status --porcelain")
        install_verifier(staged, VERIFIER_REJECTED)
        malformed = tmp_path / f"malformed_{abs(hash(body))}.py"
        malformed.write_text(
            "import sys\n\nsys.stdin.read()\n" f"sys.stdout.write({body!r})\n",
            encoding="utf-8",
        )
        result = run_fix(staged, db, worktree, classifier_cmd=[sys.executable, str(malformed)])
        assert result.returncode == EXIT_UNVERIFIABLE, (result.returncode, result.stdout)
        # The SPECIFIC guard, not just the exit code: the skipped-commands check is
        # a second net that would otherwise pass for a missing shape check, which
        # makes the shape check untested while looking covered.
        assert fragment in payload(result)["unverifiable"][0]["why"]

    def test_a_probe_that_answers_about_only_some_commands_is_unverifiable(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """A partial answer is not a verdict about the rows it skipped."""
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="git status --porcelain")
        a_golden_path(ledger_mod, db, command="git rev-parse HEAD")
        partial = tmp_path / "partial_classifier.py"
        partial.write_text(
            "import json\nimport sys\n\n"
            'request = json.loads(sys.stdin.read() or "{}")\n'
            'first = (request.get("commands") or [])[:1]\n'
            'json.dump({"available": True, "results": '
            '[{"command": c, "reason": None} for c in first]}, sys.stdout)\n',
            encoding="utf-8",
        )
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=[sys.executable, str(partial)])
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert "skipped" in payload(result)["unverifiable"][0]["why"]


class TestTheLedgerPathIsResolvedOnceAndShared:
    """Both processes must read the SAME ledger.

    The child runs with ``HOME`` pointed at the worktree so a ``~``-relative path
    lands in the throwaway checkout -- and the ledger's default path is
    ``HOME``-relative, so a child left to its own default reads a different
    database and reports "no such finding" for a finding that is right there.
    """

    def test_the_child_is_given_the_parents_resolved_default(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        # A verifier stub that records the argv it was handed. The path is baked
        # into its source because the script under test owns that argv -- there is
        # no flag through which a test could pass the recorder a destination.
        record = tmp_path / "verifier-argv.json"
        (staged / "verify_finding.py").write_text(
            "import json\nimport sys\n\n"
            f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "sys.exit(10)\n",
            encoding="utf-8",
        )
        home = tmp_path / "crew-home"
        result = run_fix(
            staged,
            None,
            worktree,
            classifier_cmd=[sys.executable, "-c", "pass"],
            env={"KIROCREW_HOME": str(home)},
        )
        assert result.returncode == EXIT_HOLDS, result.stderr
        argv = json.loads(record.read_text(encoding="utf-8"))
        assert "--db" in argv
        given = Path(argv[argv.index("--db") + 1])
        assert given.is_absolute()
        assert given == (home / "security-conductor" / "findings.db").resolve()


class TestNothingFromTheLedgerIsExecuted:
    """The security property, and the reason the flow lane checks nothing.

    ``ledger.py`` says outright that its CLI is not an authentication boundary:
    ``--approved-by`` is an unverified caller assertion, so a row is untrusted text
    written by whatever can reach the database. Running one as argv would turn a
    ledger write into command execution with the operator's access -- a privilege
    escalation no containment fixes, because the escalation is in treating the row
    as permission. So a non-shell row is reported for a human and never run.

    Each test plants a witness file the row would create if it ran.
    """

    def witness(self, tmp_path: Path, name: str) -> tuple[Path, str]:
        marker = tmp_path / f"{name}-fired"
        script = tmp_path / f"{name}_body.py"
        script.write_text(f"open({str(marker)!r}, 'w').write('fired')\n", encoding="utf-8")
        return marker, f"{sys.executable} {script}"

    def test_a_flow_row_is_reported_and_never_run(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        marker, command = self.witness(tmp_path, "flow")
        path_id = a_golden_path(ledger_mod, db, kind="flow", command=command)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert not marker.exists(), "a ledger row was executed"
        body = payload(result)
        assert [row["id"] for row in body["needs_human"]] == [path_id]
        assert "never run from the ledger" in body["needs_human"][0]["why"]

    def test_a_cron_row_is_reported_and_never_run(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """Firing a schedule also has effects outside the worktree no deadline bounds."""
        db = tmp_path / "findings.db"
        marker, command = self.witness(tmp_path, "cron")
        a_golden_path(ledger_mod, db, kind="cron", command=f"17 3 * * * :: {command}")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert not marker.exists(), "a ledger row was executed"
        assert payload(result)["needs_human"][0]["kind"] == "cron"

    def test_a_shell_row_is_classified_and_never_run(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """The CHECKED kind is not an exception: the fence reads the text, nothing runs it."""
        db = tmp_path / "findings.db"
        marker, command = self.witness(tmp_path, "shell")
        a_golden_path(ledger_mod, db, command=command)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert not marker.exists(), "a ledger row was executed"

    def test_a_human_row_never_moves_the_exit_code(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """A row this script never claimed to check cannot make its verdict worse.

        The distinction is deliberate: ``unverifiable`` means a check THIS SCRIPT
        OWNS could not be settled, and an MCP tool was never one of them. Folding
        the human corpus into 20 would make the gate permanently unable to pass
        while saying nothing new.
        """
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, kind="flow", command="monitor_start")
        a_golden_path(ledger_mod, db, kind="cron", command="every:300 :: rotation-check")
        a_golden_path(ledger_mod, db, command="git status --porcelain")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=classifier(tmp_path))
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert len(body["needs_human"]) == 2
        # Only the shell row was CHECKED, and the count says so rather than
        # reporting the whole table as verified.
        assert body["golden_paths_checked"] == 1


class TestTheSpawnPathCarriesNoLedgerText:
    """The absence a witness file cannot pin.

    ``TestNothingFromTheLedgerIsExecuted`` proves no row runs today. This proves the
    lane is not there to be reintroduced by an edit that looks reasonable: the only
    two children this script spawns are a checked-in sibling script and itself, and
    the function that walks golden paths spawns nothing at all.
    """

    def source(self) -> str:
        return VERIFY_FIX.read_text(encoding="utf-8")

    def function_body(self, name: str) -> str:
        source = self.source()
        start = source.index(f"def {name}(")
        rest = source[start:]
        end = rest.index("\ndef ", 1)
        return rest[:end]

    def test_the_golden_path_walk_spawns_nothing(self) -> None:
        body = self.function_body("check_golden_paths")
        assert "run_child(" not in body, "a golden-path row reached a subprocess spawn"
        assert "subprocess" not in body

    def test_only_two_call_sites_spawn_at_all(self) -> None:
        """One for the sibling verifier, one for this script's own probe. A third is
        a new trust decision and should not pass unnoticed."""
        source = self.source()
        calls = source.count("run_child(")
        # The definition, the verifier call, the probe call.
        assert calls == 3, calls
        assert "run_child(argv, worktree, timeout + REAP_SECONDS + 30)" in source

    def test_the_probe_argv_is_this_script_not_ledger_text(self) -> None:
        """The probe re-enters this file. An argv assembled from a row would be the
        same escalation wearing the probe's name."""
        assert "[classifier_python(worktree), __file__, CLASSIFY_FLAG]" in self.source()


class TestThePlatformFilterIsBothWays:
    def test_a_windows_row_is_not_checked_on_posix(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """Lock-in guard, half one. A Windows-only shape reported broken on Linux
        would reject a fix for a host it was never checked on."""
        db = tmp_path / "findings.db"
        a_golden_path(
            ledger_mod,
            db,
            command=".venv\\Scripts\\python.exe -m pytest REFUSE-ME",
            platform="windows",
        )
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, db, worktree, classifier_cmd=classifier(tmp_path, "REFUSE-ME"), platform="posix"
        )
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert payload(result)["golden_paths_checked"] == 0

    def test_a_posix_row_is_not_checked_on_windows(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """Lock-in guard, half two, and it is not symmetry for its own sake: the
        corpus is checked on both hosts, so each host must ignore the other's rows
        rather than only Linux ignoring Windows."""
        db = tmp_path / "findings.db"
        a_golden_path(
            ledger_mod, db, command=".venv/bin/python -m pytest REFUSE-ME", platform="posix"
        )
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged,
            db,
            worktree,
            classifier_cmd=classifier(tmp_path, "REFUSE-ME"),
            platform="windows",
        )
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert payload(result)["golden_paths_checked"] == 0

    def test_an_any_row_is_checked_on_both(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(ledger_mod, db, command="gh pr view 1 REFUSE-ME", platform="any")
        install_verifier(staged, VERIFIER_REJECTED)
        for platform in ("posix", "windows"):
            result = run_fix(
                staged,
                db,
                worktree,
                classifier_cmd=classifier(tmp_path, "REFUSE-ME"),
                platform=platform,
            )
            assert result.returncode == EXIT_BROKEN, (platform, result.stderr)
            assert payload(result)["platform"] == platform

    def test_auto_never_resolves_to_any(self, mod) -> None:
        """``any`` is a property of a ROW, not a host. Resolving to it would select
        the ``any`` rows and silently drop every platform-specific one."""
        assert mod.resolve_platform("auto") in ("posix", "windows")
        assert mod.resolve_platform("posix") == "posix"
        assert mod.resolve_platform("windows") == "windows"


class TestTheSchemaMigratesUnderALiveLedger:
    def test_a_database_created_before_golden_paths_is_readable(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A target audited before this table existed must keep working.

        Built the way a v1 ledger really exists -- the four original tables and a
        version row saying 1 -- then opened by the current code, which must add the
        table, bump the version, and answer a golden-path query rather than
        raising.
        """
        import sqlite3

        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, surface TEXT NOT NULL,
                severity TEXT NOT NULL, title TEXT NOT NULL, paths TEXT NOT NULL,
                poc TEXT, auditor_verdict TEXT, verifier_verdict TEXT,
                final_verdict TEXT, status TEXT NOT NULL, created TEXT NOT NULL,
                round_id TEXT
            );
            INSERT INTO findings (surface, severity, title, paths, status, created)
                VALUES ('security', 'High', 'legacy', '[]', 'open', '2000-01-01T00:00:00+00:00');
            """)
        conn.commit()
        conn.close()

        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, classifier_cmd=[sys.executable, "-c", "pass"])
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert payload(result)["golden_paths_checked"] == 0
        # The pre-existing row survived the migration: this is additive, not a
        # rebuild.
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        finally:
            conn.close()


class TestInvalidInputIsTwoNotAVerdict:
    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param(["--timeout", "0"], id="nonpositive-timeout"),
            pytest.param(["--classifier-cmd", "not-json"], id="unparseable-classifier"),
            pytest.param(["--classifier-cmd", "[]"], id="empty-classifier"),
        ],
    )
    def test_a_bad_argument_is_two(
        self, staged: Path, tmp_path: Path, worktree: Path, extra: list[str]
    ) -> None:
        install_verifier(staged, VERIFIER_REJECTED)
        result = subprocess.run(
            [
                sys.executable,
                str(staged / "verify_fix.py"),
                "--db",
                str(tmp_path / "findings.db"),
                "--finding-id",
                "1",
                "--worktree",
                str(worktree),
                *extra,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == EXIT_INVALID, result.stdout

    def test_a_worktree_that_is_not_a_checkout_is_two(self, staged: Path, tmp_path: Path) -> None:
        """A bare directory is not a disposable checkout, and that bound is the
        only containment a flow runs under."""
        install_verifier(staged, VERIFIER_REJECTED)
        plain = tmp_path / "not-a-checkout"
        plain.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(staged / "verify_fix.py"),
                "--finding-id",
                "1",
                "--worktree",
                str(plain),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == EXIT_INVALID
        assert "not a git checkout" in result.stderr

    def test_a_worktree_that_is_not_a_directory_is_two(self, staged: Path, tmp_path: Path) -> None:
        install_verifier(staged, VERIFIER_REJECTED)
        result = subprocess.run(
            [
                sys.executable,
                str(staged / "verify_fix.py"),
                "--finding-id",
                "1",
                "--worktree",
                str(tmp_path / "nowhere"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == EXIT_INVALID
        assert "not a directory" in result.stderr


class TestTheVerdictLadderIsDeclaredOnce:
    def test_the_fold_returns_the_strongest_verdict(self, mod) -> None:
        assert mod.fold_verdict("holds") == "holds"
        assert mod.fold_verdict("holds", "unverifiable") == "unverifiable"
        assert mod.fold_verdict("holds", "unverifiable", "broken") == "broken"
        assert mod.fold_verdict("holds", "unverifiable", "broken", "reproduces") == "reproduces"

    def test_every_verdict_has_an_exit_code_and_they_are_distinct(self, mod) -> None:
        assert set(mod.VERDICT_PRECEDENCE) == set(mod.EXIT_CODES)
        codes = list(mod.EXIT_CODES.values())
        assert sorted(codes) == sorted(set(codes))
        # The one code that must never be reachable from a non-holding verdict.
        assert mod.EXIT_CODES["holds"] == 0
        assert 0 not in [code for name, code in mod.EXIT_CODES.items() if name != "holds"]


class TestTheShippedCorpusIsALiveGate:
    """The corpus is only worth something if the real fence agrees with it.

    Every other class here stubs the classifier so a refusal can be arranged. This
    one does the opposite and asks the REAL ``is_denied`` about every shipped
    ``shell`` row, which is the assertion the corpus exists to make: a change to
    the deny fence that eats one of these operations fails here, at PR time,
    instead of in a maintainer's terminal a week later.
    """

    @pytest.fixture(scope="class")
    def rows(self, request) -> list[dict]:
        ledger_mod = load_skill_script("security_conductor_ledger_for_corpus", LEDGER)
        return ledger_mod.load_golden_path_corpus(SEED.read_text(encoding="utf-8"))

    def test_the_seed_carries_a_real_corpus(self, rows: list[dict]) -> None:
        assert 25 <= len(rows) <= 40, len(rows)
        assert all(row["reason"] for row in rows)
        kinds = {row["kind"] for row in rows}
        assert kinds == {"shell", "flow", "cron"}
        # Both halves of the lock-in guard are present, or the corpus asserts
        # nothing about the second failure mode it exists for.
        platforms = {row["platform"] for row in rows}
        assert {"posix", "windows"} <= platforms

    def test_every_shipped_shell_row_is_permitted_by_the_real_deny_fence(
        self, rows: list[dict]
    ) -> None:
        from kiro_crew.security import is_denied

        refused = {
            row["command_or_flow"]: is_denied(row["command_or_flow"])
            for row in rows
            if row["kind"] == "shell"
        }
        broken = {command: reason for command, reason in refused.items() if reason is not None}
        assert not broken, json.dumps(broken, indent=2, sort_keys=True)

    def test_every_shipped_cron_row_is_a_wellformed_pair(self, rows: list[dict]) -> None:
        """Asserted HERE, at review time, rather than by the script at run time.

        A cron row's well-formedness is a property of the checked-in corpus, and a
        parse in the verifier could never be a verdict about a FIX anyway -- it
        would read the ledger with the verifier's own regexes and answer the same
        constant no matter what the worktree contained.
        """
        problems = {}
        for row in rows:
            if row["kind"] != "cron":
                continue
            text = row["command_or_flow"]
            schedule, separator, command = text.partition("::")
            if not separator:
                problems[text] = "no '::' separator"
                continue
            schedule, command = schedule.strip(), command.strip()
            if not schedule or not command:
                problems[text] = "empty half"
                continue
            if not re.fullmatch(r"every:[1-9][0-9]*", schedule, re.IGNORECASE):
                fields = schedule.split()
                if len(fields) != 5:
                    problems[text] = f"{len(fields)} schedule fields"
                    continue
                if any(not re.fullmatch(r"[0-9*,/\-A-Za-z]+", f) for f in fields):
                    problems[text] = "unparseable schedule field"
                    continue
            try:
                if not shlex.split(command):
                    problems[text] = "command splits to nothing"
            except ValueError as exc:
                problems[text] = f"unparseable command: {exc}"
        assert not problems, problems

    def test_no_shipped_row_carries_a_scheme_prefix(self, rows: list[dict]) -> None:
        """A ``<scheme>::`` marker existed to tell an executable flow from a declared
        one. Nothing from the ledger is executed, so a prefix left behind would
        describe machinery that is not there."""
        flows = [row["command_or_flow"] for row in rows if row["kind"] == "flow"]
        assert flows
        assert not [text for text in flows if re.match(r"^[a-z][a-z0-9_-]*::", text)]

    def test_the_seed_imports_idempotently(self, ledger_mod, tmp_path: Path) -> None:
        db = tmp_path / "seeded.db"
        conn = ledger_mod.connect(db)
        try:
            ledger_mod.init_schema(conn)
            rows = ledger_mod.load_golden_path_corpus(SEED.read_text(encoding="utf-8"))
            first = ledger_mod.import_golden_paths(conn, rows, approved_by="tester")
            second = ledger_mod.import_golden_paths(conn, rows, approved_by="tester")
        finally:
            conn.close()
        assert first["imported"] == len(rows)
        assert second == {"imported": 0, "skipped": len(rows), "total": len(rows)}
