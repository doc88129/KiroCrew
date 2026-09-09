"""The Advisor reviewer's private agents directory is sealed like ``~/.kiro/agents``.

The reviewer runs with ``KIRO_HOME=<kiro home>/kirocrew-advisor`` and resolves its
``--agent`` spec from ``<that home>/agents``. The spec is what makes the reviewer
toolless (``"tools": []``), so a sandboxed primary agent that could rewrite it
would hand the next reviewer spawn forged tools. The operator's own agents tree is
already sealed four ways for exactly that reason; these tests pin that the
reviewer's tree gets every one of the same seals:

* the file-edit tool gate (``is_sensitive_write_path``), anchored under the
  default home AND re-anchored under a ``KIRO_HOME`` override;
* the OS seal both launchers apply (Linux ``READONLY_DIRS``, macOS Seatbelt
  ``deny file-write*``), resolved through the same accessor as the operator's
  agents tree so a workspace overlapping it is refused on delegated platforms;
* the Linux pre-create step, so the seal has a mount target BEFORE the advisor
  is ever enabled -- a namespace built while the directory was absent would
  otherwise leave it writable for the life of that session.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kiro_crew import sandbox, security
from kiro_crew.config import paths as config_paths
from kiro_crew.config.paths import advisor_agents_dir, advisor_kiro_home, kiro_agents_dir
from kiro_crew.security import paths as security_paths


@pytest.fixture
def isolated_homes(tmp_path, monkeypatch):
    """A fake ``$HOME`` with a crew data home and a kiro home, no advisor home yet."""
    home = tmp_path / "home"
    (home / ".kiro" / "crew").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # ``os.path.expanduser`` reads USERPROFILE on Windows, never HOME; the gate
    # expands ``~`` with it while the targets anchor on ``Path.home()``.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(home / ".kiro" / "crew"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(sandbox, "config_dir", lambda: home / ".kiro" / "crew")
    if hasattr(config_paths, "_agents_dir_override"):
        monkeypatch.setattr(config_paths, "_agents_dir_override", None)
    security_paths._home_targets_cache.clear()
    return home


class TestSingleSourceOfTruth:
    def test_advisor_home_is_a_child_of_the_kiro_home(self, isolated_homes):
        assert advisor_kiro_home() == isolated_homes / ".kiro" / "kirocrew-advisor"
        assert advisor_kiro_home(Path("/x")) == Path("/x") / "kirocrew-advisor"
        assert advisor_agents_dir() == advisor_kiro_home() / "agents"

    def test_oneshot_delegates_to_the_config_accessor(self, isolated_homes):
        # ``sandbox`` cannot import ``agent_sdk.oneshot`` (oneshot imports sandbox),
        # so the layout lives in ``config.paths``; oneshot must not carry a second
        # spelling that could drift away from the sealed one.
        from kiro_crew.agent_sdk import oneshot

        assert oneshot.advisor_kiro_home(Path("/x")) == advisor_kiro_home(Path("/x"))


class TestFileEditToolGate:
    def test_literal_pins_the_default_layout(self, isolated_homes):
        rel = advisor_agents_dir().relative_to(Path.home()).as_posix()
        assert security._ADVISOR_AGENTS_DIR == rel

    def test_write_into_the_reviewer_agents_dir_is_denied(self, isolated_homes):
        assert security.is_sensitive_write_path("~/.kiro/kirocrew-advisor/agents/x.json") is True
        assert security.is_sensitive_write_path("~/.kiro/kirocrew-advisor/agents") is True
        assert (
            security.is_sensitive_write_path("~/.kiro/kirocrew-advisor/agents/deep/spec.json")
            is True
        )

    def test_reviewer_session_store_stays_writable(self, isolated_homes):
        # kiro-cli persists the reviewer's sessions beside the agents dir; only
        # the spec tree is fenced.
        assert (
            security.is_sensitive_write_path("~/.kiro/kirocrew-advisor/sessions/cli/s.json")
            is False
        )

    def test_reads_are_not_fenced(self, isolated_homes):
        assert security.is_sensitive_path("~/.kiro/kirocrew-advisor/agents/x.json") is False

    def test_kiro_home_override_is_re_anchored(self, tmp_path, monkeypatch, isolated_homes):
        other = tmp_path / "other-kiro"
        other.mkdir()
        monkeypatch.setenv("KIRO_HOME", str(other))
        security_paths._home_targets_cache.clear()
        target = other / "kirocrew-advisor" / "agents" / "x.json"
        assert security.is_sensitive_write_path(str(target)) is True


class TestOsSeal:
    def test_resolved_targets_carry_both_agents_trees(self, isolated_homes):
        targets = sandbox._resolved_kiro_agents_targets()
        assert os.path.normpath(str(kiro_agents_dir())) in targets
        assert os.path.normpath(str(advisor_agents_dir())) in targets

    def test_seatbelt_profile_denies_writes_to_it(self, isolated_homes):
        profile = sandbox._build_seatbelt_profile("strict")
        target = str(advisor_agents_dir())
        assert f'(deny file-write* (subpath "{target}"))' in profile
        assert f'(deny file-link (subpath "{target}"))' in profile

    def test_seatbelt_profile_pins_the_home_entry_but_not_its_contents(self, isolated_homes):
        # Seatbelt matches the RESOLVED path, so the ``agents`` deny holds only while
        # the parent is a real directory: the entry's own name must be un-creatable,
        # un-renamable and un-unlinkable, while ``sessions/`` beneath stays writable
        # for the reviewer's kiro-cli.
        profile = sandbox._build_seatbelt_profile("strict")
        home = str(advisor_kiro_home())
        assert f'(deny file-write* (literal "{home}"))' in profile
        assert f'(deny file-link (literal "{home}"))' in profile
        assert f'(deny file-write* (subpath "{home}"))' not in profile

    def test_seatbelt_profile_pins_every_ancestor_of_the_home(self, isolated_homes):
        # The same replace-the-parent shape one level up (rename ``~/.kiro`` away and
        # plant a link) is already denied by the existing rename-sensitive ancestor
        # guards; this pins that the advisor home's whole ancestor chain is covered,
        # so the ``agents`` deny cannot be routed around from ANY level.
        profile = sandbox._build_seatbelt_profile("strict")
        current = advisor_kiro_home().parent
        while str(current) != current.anchor:
            assert f'(deny file-write* (literal "{current}"))' in profile, current
            current = current.parent

    @staticmethod
    def _assert_chain_pinned(profile: str) -> None:
        current = advisor_kiro_home()
        while str(current) != current.anchor:
            assert f'(deny file-write* (literal "{current}"))' in profile, current
            current = current.parent

    def test_ancestors_are_pinned_when_the_crew_home_is_relocated(
        self, isolated_homes, tmp_path, monkeypatch
    ):
        # The voice-runtime ancestor guards run from the CREW data home; with a
        # relocated KIROCREW_HOME that chain does not pass through ``~/.kiro``, so the
        # advisor home's chain must be pinned on its own.
        relocated = tmp_path / "data" / "crew"
        relocated.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(relocated))
        monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
        sandbox._voice_runtime_paths_cache = None
        profile = sandbox._build_seatbelt_profile("strict")
        assert f'(deny file-write* (literal "{isolated_homes / ".kiro"}"))' in profile
        self._assert_chain_pinned(profile)

    def test_ancestors_are_pinned_under_a_kiro_home_override(
        self, isolated_homes, tmp_path, monkeypatch
    ):
        other = tmp_path / "other-kiro"
        other.mkdir()
        monkeypatch.setenv("KIRO_HOME", str(other))
        profile = sandbox._build_seatbelt_profile("strict")
        assert advisor_kiro_home() == other / "kirocrew-advisor"
        assert f'(deny file-write* (literal "{other}"))' in profile
        self._assert_chain_pinned(profile)

    def test_home_pin_is_a_literal_guard_against_writable_carveouts(self, isolated_homes):
        # Carve-out allows are emitted LAST (last-match-wins), so the pin must be in the
        # guard set the carve-out validator refuses, or a relocated KIRO_HOME beneath a
        # carveable runtime parent could reopen the entry for writes.
        seen: dict[str, list[str]] = {}
        real = sandbox._writable_carveout_spellings

        def spy(extra_writable_dirs, **kwargs):
            seen["literal_guards"] = list(kwargs["literal_guards"])
            return real(extra_writable_dirs, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "_writable_carveout_spellings", spy)
            sandbox._build_seatbelt_profile("strict")
        assert str(advisor_kiro_home()) in seen["literal_guards"]

    def test_seatbelt_path_materializes_the_home_and_refuses_a_link(self, isolated_homes, tmp_path):
        sandbox._materialize_advisor_kiro_home_if_installed()
        assert advisor_kiro_home().is_dir()
        if os.name != "nt":  # POSIX mode bits; Windows reports 0o777 for every dir
            assert stat.S_IMODE(os.stat(advisor_kiro_home()).st_mode) == 0o700
        os.rmdir(advisor_kiro_home())
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, advisor_kiro_home())
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_advisor_kiro_home_if_installed()

    def test_seatbelt_spawn_path_materializes_before_writing_the_profile(
        self, isolated_homes, monkeypatch
    ):
        # Pins the wiring, not just the helper: a refactor that dropped the call
        # from ``sandbox_exec_argv`` would leave the literal deny guarding a name
        # the gateway never made real.
        order: list[str] = []
        real = sandbox._materialize_advisor_kiro_home_if_installed
        monkeypatch.setattr(
            sandbox,
            "_materialize_advisor_kiro_home_if_installed",
            lambda: (order.append("materialize"), real())[1],
        )
        argv, profile_path = sandbox.sandbox_exec_argv(["echo", "hi"], "strict")
        try:
            assert order == ["materialize"]
            assert advisor_kiro_home().is_dir()
            profile = Path(profile_path).read_text(encoding="utf-8")
            assert f'(deny file-write* (literal "{advisor_kiro_home()}"))' in profile
        finally:
            if profile_path:
                os.unlink(profile_path)

    def test_delegated_platform_refuses_a_workspace_inside_the_advisor_agents_dir(
        self, isolated_homes, monkeypatch
    ):
        # The operator's tree does not overlap this workspace; only the second
        # resolved target does, so this pins that every target is consulted.
        monkeypatch.setattr(sandbox.sys, "platform", "win32")
        agents = advisor_agents_dir()
        agents.mkdir(parents=True)
        reason = sandbox.delegated_workspace_exposes_agents_dir(str(agents / "proj"))
        assert reason is not None
        assert "Advisor reviewer's agents directory" in reason
        assert "toolless reviewer spec" in reason
        assert sandbox.delegated_workspace_exposes_agents_dir(str(isolated_homes / "ok")) is None


class TestInstaller:
    def test_install_refuses_to_write_through_a_linked_home(self, isolated_homes, tmp_path):
        from kiro_crew.advisor import composition

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, advisor_kiro_home())
        with pytest.raises(composition.AdvisorSpecError):
            composition.ensure_advisor_agent_installed()
        assert not (elsewhere / "agents").exists()

    def test_install_refuses_to_write_through_a_linked_agents_leaf(self, isolated_homes, tmp_path):
        from kiro_crew.advisor import composition

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        advisor_kiro_home().mkdir()
        os.symlink(elsewhere, advisor_agents_dir())
        with pytest.raises(composition.AdvisorSpecError):
            composition.ensure_advisor_agent_installed()
        assert list(elsewhere.iterdir()) == []

    def test_install_still_succeeds_into_the_precreated_real_dirs(self, isolated_homes):
        from kiro_crew.advisor import composition

        sandbox._materialize_sealable_ceilings()
        target = composition.ensure_advisor_agent_installed()
        assert target.parent == advisor_agents_dir()
        assert target.is_file()


class TestLinuxPrecreate:
    def test_absent_advisor_home_and_agents_dir_are_created_for_the_seal(self, isolated_homes):
        dirs, _files = sandbox._sealable_absent_ceilings()
        target = str(advisor_agents_dir())
        assert target in dirs
        created = sandbox._materialize_sealable_ceilings()
        assert target in created
        assert os.path.isdir(target)
        if os.name != "nt":  # POSIX mode bits; Windows reports 0o777 for every dir
            assert stat.S_IMODE(os.stat(os.path.dirname(target)).st_mode) == 0o700

    def test_existing_advisor_home_is_left_alone(self, isolated_homes):
        home = advisor_kiro_home()
        (home / "sessions" / "cli").mkdir(parents=True)
        (home / "sessions" / "cli" / "s.json").write_text("{}")
        sandbox._materialize_sealable_ceilings()
        assert (home / "sessions" / "cli" / "s.json").read_text() == "{}"
        assert (home / "agents").is_dir()

    def test_a_link_planted_at_the_advisor_home_refuses_the_spawn(self, isolated_homes, tmp_path):
        # The reviewer resolves its spec THROUGH this component, so a link here
        # would let a sandboxed writer swap the whole agents tree from under the
        # seal after launch.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, advisor_kiro_home())
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_a_resolving_link_at_the_agents_leaf_refuses_the_spawn(self, isolated_homes, tmp_path):
        # Strict by PATH: the operator's ``~/.kiro/agents`` keeps its warn-only
        # alias posture (dotfile managers), but this leaf is product-managed, so a
        # link at it has no legitimate author and would let a sandboxed process
        # unlink the name and put its own directory there after the seal mounted.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        advisor_kiro_home().mkdir()
        os.symlink(elsewhere, advisor_agents_dir())
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_a_link_winning_the_agents_create_race_refuses(
        self, isolated_homes, tmp_path, monkeypatch
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        real_mkdir = os.mkdir
        target = str(advisor_agents_dir())

        def racing_mkdir(path, mode=0o777, *args, **kwargs):
            if os.fspath(path) == target:
                os.symlink(elsewhere, target)  # the racer wins with a link
                raise FileExistsError(target)
            return real_mkdir(path, mode, *args, **kwargs)

        monkeypatch.setattr(sandbox.os, "mkdir", racing_mkdir)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_absent_kiro_home_is_not_scaffolded(self, isolated_homes):
        import shutil

        shutil.rmtree(isolated_homes / ".kiro" / "crew")
        # No crew data home: nothing is created, exactly as for the operator's agents dir.
        dirs, _ = sandbox._sealable_absent_ceilings()
        assert str(advisor_agents_dir()) not in dirs
