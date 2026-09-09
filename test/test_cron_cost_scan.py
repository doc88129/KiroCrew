"""Tests for the cron-cost-optimize scan helper.

The scan tells a user to change a job that already works, so the tests pin the
direction of every wrong answer it could give. Two properties matter more than
the happy path:

* A job that needs a model must never be told to become a script, because that
  failure is silent -- the rewritten job stays green and quietly stops doing its
  work. Every judgement-shaped prompt here has to fall back, not through.
* A verdict must be derived from recorded runs, not from adjectives. The history
  fixtures below are the evidence, and thin history has to downgrade confidence
  rather than be ignored.

The four prompts in `test_ticket_examples_are_caught` are the real cases from the
ASBX cost analysis, kept verbatim so a pattern change cannot quietly stop
catching the traffic this skill was written for.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "cron-cost-optimize"
    / "scripts"
    / "cron_cost_scan.py"
)


def _load() -> Any:
    """Import the script by path -- it ships inside a skill dir, not a package.

    The module has to be registered in ``sys.modules`` before it executes, because
    ``dataclass`` resolves its own field annotations through that table.
    """
    spec = importlib.util.spec_from_file_location("cron_cost_scan", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _job(**over: Any) -> dict[str, Any]:
    """A job record shaped like the store writes it, with every default present."""
    rec: dict[str, Any] = {
        "id": "job1",
        "name": "a-job",
        "message": "Do the thing.",
        "schedule": {"kind": "every", "every_secs": 600, "at_ts": None, "cron_expr": None},
        "enabled": True,
        "user_paused": False,
        "auto_paused": False,
        "script": "",
        "command": "",
        "minimal_context": False,
        "persistent_session": True,
        "hide_in_chat": False,
        "last_result": "",
    }
    rec.update(over)
    return rec


def _runs(*summaries: str, failures: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"status": "success", "summary": s} for s in summaries]
    out += [{"status": "failure", "summary": "", "error": "boom"} for _ in range(failures)]
    return out


def _classify(job: dict[str, Any], records: list[dict[str, Any]], min_runs: int = 5) -> Any:
    return mod.classify(job, mod.summarize_history(records), min_runs)


class TestZeroTokenJobsAreLeftAlone(unittest.TestCase):
    def test_script_job(self) -> None:
        f = _classify(_job(script="~/.kiro/crew/crons/poll.py:check"), [])
        self.assertEqual(f.verdict, "already-zero-token")
        self.assertEqual(f.mode, "script")
        self.assertEqual(f.change, "")

    def test_command_job(self) -> None:
        f = _classify(_job(command="df -h"), [])
        self.assertEqual(f.verdict, "already-zero-token")
        self.assertEqual(f.mode, "command")

    def test_script_wins_over_a_leftover_prompt(self) -> None:
        """A script job keeps its message field; the mode is what decides cost."""
        f = _classify(_job(script="~/.kiro/crew/crons/x.py:go", message="Summarize the day."), [])
        self.assertEqual(f.verdict, "already-zero-token")


class TestScriptVerdict(unittest.TestCase):
    def test_deterministic_prompt_with_idle_history_is_high_confidence(self) -> None:
        job = _job(message="Compare the channel timestamp with the last run and report a change.")
        f = _classify(job, _runs(*["No new messages. Timestamp unchanged."] * 8))
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "high")
        self.assertIn("8", f.reason)

    def test_thin_history_downgrades_to_low_confidence(self) -> None:
        job = _job(message="Check whether the disk usage is above 80 percent.")
        f = _classify(job, _runs("Clean run."))
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "low")
        self.assertIn("not enough run history", f.reason)

    def test_no_history_at_all_still_reports_low_not_nothing(self) -> None:
        f = _classify(_job(message="Check the exit code of the health probe."), [])
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "low")

    def test_unremarkable_prompt_with_identical_history_is_medium(self) -> None:
        """History outranks wording: a job repeating itself is doing no reasoning."""
        job = _job(message="Look at the queue and tell me about it.")
        f = _classify(job, _runs(*["Queue is fine."] * 9))
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "medium")

    def test_dedup_carry_is_flagged_when_the_session_persists(self) -> None:
        job = _job(message="Check the timestamp.", persistent_session=True)
        f = _classify(job, _runs(*["Unchanged."] * 6))
        self.assertEqual(f.verdict, "move-to-script")
        self.assertTrue(any("dedup" in n for n in f.notes))

    def test_no_dedup_note_without_a_persistent_session(self) -> None:
        job = _job(message="Check the timestamp.", persistent_session=False)
        f = _classify(job, _runs(*["Unchanged."] * 6))
        self.assertFalse(any("dedup" in n for n in f.notes))


class TestJudgementJobsNeverBecomeScripts(unittest.TestCase):
    """The one wrong answer that fails silently, so every case must fall back."""

    def test_summarize_falls_back_to_minimal_context(self) -> None:
        job = _job(message="Summarize the new messages and check the timestamp.")
        f = _classify(job, _runs(*["Nothing new."] * 20))
        self.assertEqual(f.verdict, "enable-minimal-context")
        self.assertTrue(f.blockers)

    def test_every_judgement_word_blocks_the_rewrite(self) -> None:
        for verb in (
            "Summarize the disk usage",
            "Review the file size",
            "Draft a note about the exit code",
            "Triage the timestamp",
            "Analyze the port status",
            "Recommend a threshold",
            "Investigate the checksum",
            "Reply to anything above 5",
        ):
            with self.subTest(verb=verb):
                f = _classify(_job(message=verb + "."), _runs(*["Nothing to do."] * 12))
                self.assertNotEqual(f.verdict, "move-to-script")

    def test_idle_history_cannot_override_a_judgement_prompt(self) -> None:
        """Even 40 identical no-op runs must not unlock a script rewrite."""
        job = _job(message="Review the open items and summarize what changed.")
        f = _classify(job, _runs(*["Nothing to report."] * 40))
        self.assertNotEqual(f.verdict, "move-to-script")


class TestMinimalContextBlockers(unittest.TestCase):
    def test_dollar_skill_token_blocks_minimal_context(self) -> None:
        job = _job(message="Run $babysit against the open pull request and report.")
        f = _classify(job, _runs(*["Nothing new."] * 10))
        self.assertEqual(f.verdict, "leave-as-is")
        self.assertTrue(any("skill" in b for b in f.blockers))

    def test_skill_named_in_prose_blocks_minimal_context(self) -> None:
        job = _job(message="Load the prepare-pr skill and drive the branch to green.")
        f = _classify(job, _runs(*["Still red."] * 10))
        self.assertEqual(f.verdict, "leave-as-is")

    def test_memory_dependence_blocks_minimal_context(self) -> None:
        job = _job(message="Using my saved preferences, decide what to escalate.")
        f = _classify(job, _runs(*["Nothing."] * 10))
        self.assertEqual(f.verdict, "leave-as-is")
        self.assertTrue(any("preference" in b for b in f.blockers))

    def test_already_minimal_and_still_reasoning_is_left_alone(self) -> None:
        job = _job(message="Summarize the new failures.", minimal_context=True)
        f = _classify(job, _runs(*["Two new failures."] * 10))
        self.assertEqual(f.verdict, "leave-as-is")
        self.assertIn("already on minimal context", f.reason)

    def test_hide_in_chat_is_offered_only_when_runs_are_noise(self) -> None:
        job = _job(message="Summarize anything new.", hide_in_chat=False)
        quiet = _classify(job, _runs(*["Nothing new."] * 10))
        self.assertEqual(quiet.verdict, "enable-minimal-context")
        self.assertTrue(any("hide_in_chat" in n for n in quiet.notes))

        busy = _classify(job, _runs(*[f"Found {i} new items." for i in range(10)]))
        self.assertEqual(busy.verdict, "enable-minimal-context")
        self.assertFalse(any("hide_in_chat" in n for n in busy.notes))


class TestTicketExamples(unittest.TestCase):
    def test_ticket_examples_are_caught(self) -> None:
        """The four real cases from the cost analysis must all leave full-context mode."""
        cases = [
            # 101K tokens to compare two integers.
            ("Check the channel timestamp and report only if it changed.", "move-to-script"),
            # 100K tokens for a threshold check.
            (
                "Check disk usage for tmp and home and report if either is above 80.",
                "move-to-script",
            ),
            # 78K tokens re-deriving a documented permanent limitation.
            (
                "Check whether the upstream fix already exists and report the status code.",
                "move-to-script",
            ),
            # 141K tokens for what a grep settles. The artifact is called a triage
            # comment, so the judgement guard fires on the word and the scan falls
            # back rather than risking a silent rewrite. Still leaves full context.
            ("Check whether a First Pass Triage comment already exists.", "enable-minimal-context"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                f = _classify(_job(message=message), _runs(*["Nothing to do."] * 10))
                self.assertEqual(f.verdict, expected)
                self.assertNotEqual(f.verdict, "leave-as-is")


class TestHistoryFolding(unittest.TestCase):
    def test_digits_are_collapsed_so_a_changing_count_still_compares_equal(self) -> None:
        self.assertEqual(
            mod.normalize_summary("Disk healthy: tmp 1%, home 19%"),
            mod.normalize_summary("Disk healthy: tmp 4%, home 22%"),
        )

    def test_whitespace_and_case_are_collapsed(self) -> None:
        self.assertEqual(
            mod.normalize_summary("  No   NEW items\n"), mod.normalize_summary("no new items")
        )

    def test_same_every_run_sees_through_changing_numbers(self) -> None:
        ev = mod.summarize_history(_runs("Clean. tmp 1%", "Clean. tmp 3%", "Clean. tmp 9%"))
        self.assertTrue(ev.same_every_run)
        self.assertEqual(ev.distinct_summaries, 1)

    def test_a_single_run_is_never_same_every_run(self) -> None:
        self.assertFalse(mod.summarize_history(_runs("Clean.")).same_every_run)

    def test_failures_are_counted_apart_from_runs(self) -> None:
        ev = mod.summarize_history(_runs("ok", "ok", failures=3))
        self.assertEqual(ev.runs, 2)
        self.assertEqual(ev.failures, 3)

    def test_noop_ratio(self) -> None:
        ev = mod.summarize_history(_runs("Nothing to do.", "Nothing to do.", "Found 3 items."))
        self.assertEqual(ev.noop_runs, 2)
        self.assertAlmostEqual(ev.noop_ratio, 2 / 3)

    def test_empty_history_has_no_ratio_and_no_evidence(self) -> None:
        ev = mod.summarize_history([])
        self.assertEqual(ev.noop_ratio, 0.0)
        self.assertFalse(ev.is_evidence(5))
        self.assertFalse(ev.says_idle(1))


class TestSchedule(unittest.TestCase):
    def test_interval_becomes_wakes_per_day(self) -> None:
        label, per_day = mod.describe_schedule({"kind": "every", "every_secs": 3600})
        self.assertEqual(label, "every 3600s")
        self.assertEqual(per_day, 24.0)

    def test_cron_expression_is_shown_but_not_counted(self) -> None:
        label, per_day = mod.describe_schedule({"kind": "cron", "cron_expr": "0 9 * * 1-5"})
        self.assertEqual(label, "cron 0 9 * * 1-5")
        self.assertIsNone(per_day)

    def test_one_shot(self) -> None:
        self.assertEqual(mod.describe_schedule({"kind": "at", "at_ts": 1.0}), ("one shot", 0.0))

    def test_malformed_schedule_degrades(self) -> None:
        self.assertEqual(mod.describe_schedule(None), ("unknown", None))
        self.assertEqual(mod.describe_schedule({"kind": "every"}), ("every (interval unset)", None))


class TestStoreReading(unittest.TestCase):
    def test_missing_store_is_reported_not_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            jobs, problem = mod.load_jobs(Path(tmp))
            self.assertEqual(jobs, [])
            self.assertIn("no cron store", problem)

    def test_unparseable_store_is_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "crons.json").write_text("{not json", encoding="utf-8")
            _, problem = mod.load_jobs(Path(tmp))
            self.assertIn("cannot parse", problem)

    def test_wrong_shape_is_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "crons.json").write_text("[]", encoding="utf-8")
            _, problem = mod.load_jobs(Path(tmp))
            self.assertIn("expected an object", problem)

    def test_corrupt_history_lines_are_skipped_not_fatal(self) -> None:
        with TemporaryDirectory() as tmp:
            hist = Path(tmp) / "cron-history"
            hist.mkdir()
            (hist / "job1.jsonl").write_text(
                '{"status": "success", "summary": "ok"}\nnot json\n\n{"status": "success"}\n',
                encoding="utf-8",
            )
            records = mod.load_history(Path(tmp), "job1", 40)
            self.assertEqual(len(records), 2)

    def test_history_limit_keeps_the_most_recent(self) -> None:
        with TemporaryDirectory() as tmp:
            hist = Path(tmp) / "cron-history"
            hist.mkdir()
            lines = [json.dumps({"status": "success", "summary": str(i)}) for i in range(10)]
            (hist / "job1.jsonl").write_text("\n".join(lines), encoding="utf-8")
            records = mod.load_history(Path(tmp), "job1", 3)
            self.assertEqual([r["summary"] for r in records], ["7", "8", "9"])

    def test_absent_history_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(mod.load_history(Path(tmp), "nope", 40), [])


class TestHomeResolution(unittest.TestCase):
    def test_explicit_home_wins_over_the_environment(self) -> None:
        self.assertEqual(mod.resolve_home("/tmp/somewhere"), Path("/tmp/somewhere"))

    def test_env_override_is_honored(self) -> None:
        with mock.patch.dict("os.environ", {"KIROCREW_HOME": "/tmp/envhome"}):
            self.assertEqual(mod.resolve_home(None), Path("/tmp/envhome"))

    def test_default_location(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(mod.resolve_home(None), Path.home() / ".kiro" / "crew")


class TestEndToEnd(unittest.TestCase):
    def _home(self, tmp: str) -> Path:
        home = Path(tmp)
        jobs = [
            _job(id="a", name="poll", message="Check the timestamp for a change."),
            _job(id="b", name="digest", message="Summarize the new failures."),
            _job(id="c", name="cheap", script="~/.kiro/crew/crons/x.py:go"),
        ]
        (home / "crons.json").write_text(json.dumps({"version": 2, "jobs": jobs}), encoding="utf-8")
        hist = home / "cron-history"
        hist.mkdir()
        for job_id in ("a", "b"):
            lines = [
                json.dumps({"status": "success", "summary": "Nothing to do."}) for _ in range(8)
            ]
            (hist / f"{job_id}.jsonl").write_text("\n".join(lines), encoding="utf-8")
        return home

    def test_scan_classifies_every_job(self) -> None:
        with TemporaryDirectory() as tmp:
            findings = mod.scan(self._home(tmp), 5, 40, None)
            by_id = {f.job_id: f.verdict for f in findings}
            self.assertEqual(by_id["a"], "move-to-script")
            self.assertEqual(by_id["b"], "enable-minimal-context")
            self.assertEqual(by_id["c"], "already-zero-token")

    def test_job_filter_accepts_an_id_or_a_name(self) -> None:
        with TemporaryDirectory() as tmp:
            home = self._home(tmp)
            self.assertEqual(len(mod.scan(home, 5, 40, "a")), 1)
            self.assertEqual(len(mod.scan(home, 5, 40, "digest")), 1)
            self.assertEqual(len(mod.scan(home, 5, 40, "absent")), 0)

    def test_missing_store_raises_lookup_error_for_the_cli_to_report(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(LookupError):
                mod.scan(Path(tmp), 5, 40, None)

    def test_scan_writes_nothing(self) -> None:
        """The audit must be safe to run against a live home, so it may not write."""
        with TemporaryDirectory() as tmp:
            home = self._home(tmp)
            before = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
            stamps = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
            mod.scan(home, 5, 40, None)
            after = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
            self.assertEqual(before, after)
            for path, mtime in stamps.items():
                self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_json_output_is_valid_and_carries_the_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            findings = mod.scan(self._home(tmp), 5, 40, None)
            payload = json.loads(mod.render_json(findings, 5))
            self.assertEqual(payload["scanned"], 3)
            self.assertEqual(payload["min_runs"], 5)
            first = payload["findings"][0]
            for key in ("job_id", "verdict", "confidence", "reason", "evidence", "blockers"):
                self.assertIn(key, first)

    def test_text_output_leads_with_counts_and_ends_with_the_caveats(self) -> None:
        with TemporaryDirectory() as tmp:
            text = mod.render_text(mod.scan(self._home(tmp), 5, 40, None), 5)
            self.assertIn("Scanned 3 cron job(s).", text)
            self.assertIn("move-to-script", text)
            self.assertIn("not a measurement", text)
            self.assertIn("Nothing was changed.", text)

    def test_cli_exits_zero_on_a_readable_home(self) -> None:
        with TemporaryDirectory() as tmp:
            home = self._home(tmp)
            self.assertEqual(mod.main(["--home", str(home), "--json"]), 0)

    def test_cli_exits_one_on_a_missing_store(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(mod.main(["--home", tmp]), 1)

    def test_cli_rejects_a_nonsense_window(self) -> None:
        with self.assertRaises(SystemExit):
            mod.main(["--min-runs", "0"])


if __name__ == "__main__":
    unittest.main()
