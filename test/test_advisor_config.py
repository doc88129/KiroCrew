"""Advisor effective enablement: global default composed with the slot override.

Contract under test (see docs/system-specs/modules/advisor.md):

- The per-session override is one of ``inherit`` / ``on`` / ``off``;
  ``inherit`` defers to the global ``advisor.enabled`` setting.
- ``on``/``off`` win over the global value in both directions.
- Unknown or empty override values behave as ``inherit`` (fail toward the
  configured default, never toward silently enabling).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.advisor.service import (
    OVERRIDE_INHERIT,
    OVERRIDE_OFF,
    OVERRIDE_ON,
    configure_from_config,
    resolve_effective_enabled,
)


class TestEffectiveEnablement:
    @pytest.mark.parametrize(
        ("global_enabled", "override", "expected"),
        [
            (False, OVERRIDE_INHERIT, False),
            (True, OVERRIDE_INHERIT, True),
            (False, OVERRIDE_ON, True),
            (True, OVERRIDE_ON, True),
            (False, OVERRIDE_OFF, False),
            (True, OVERRIDE_OFF, False),
        ],
    )
    def test_truth_table(self, global_enabled, override, expected):
        assert resolve_effective_enabled(global_enabled, override) is expected

    @pytest.mark.parametrize("bogus", ["", "banana", None, 42, "ON "])
    def test_unrecognized_override_behaves_as_inherit(self, bogus):
        assert resolve_effective_enabled(True, bogus) is True
        assert resolve_effective_enabled(False, bogus) is False


class TestAdvisorConfigSection:
    """The advisor.* config section loads, defaults, and round-trips."""

    def _load(self, data):
        import json
        import tempfile
        import unittest.mock
        from pathlib import Path

        from kiro_crew.config.loader import KiroCrewConfig

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                return KiroCrewConfig.load()
        finally:
            tmp.unlink(missing_ok=True)

    def test_absent_section_uses_defaults(self):
        cfg = self._load({})
        assert cfg.advisor.enabled is False
        assert cfg.advisor.model == ""
        assert cfg.advisor.non_blocker_budget == 4
        assert cfg.advisor.cooldown_secs == 120.0
        assert cfg.advisor.include_reasoning is False

    def test_section_values_are_parsed(self):
        cfg = self._load(
            {
                "advisor": {
                    "enabled": True,
                    "model": "reviewer-x",
                    "non_blocker_budget": 2,
                    "cooldown_secs": 30,
                    "include_reasoning": True,
                }
            }
        )
        assert cfg.advisor.enabled is True
        assert cfg.advisor.model == "reviewer-x"
        assert cfg.advisor.non_blocker_budget == 2
        assert cfg.advisor.cooldown_secs == 30.0
        assert cfg.advisor.include_reasoning is True

    def test_bad_values_fall_back_without_crashing(self):
        cfg = self._load(
            {
                "advisor": {
                    "enabled": "banana",
                    "non_blocker_budget": "x",
                    "cooldown_secs": "y",
                    "model": 42,
                }
            }
        )
        assert cfg.advisor.enabled is False
        assert cfg.advisor.non_blocker_budget == 4
        assert cfg.advisor.cooldown_secs == 120.0
        assert cfg.advisor.model == ""

    def test_to_dict_emits_the_section(self):
        cfg = self._load({"advisor": {"enabled": True}})
        assert cfg.to_dict()["advisor"]["enabled"] is True


class TestOverrideAwareAttach:
    def test_slot_on_override_attaches_despite_disabled_global(self):
        from kiro_crew.advisor.service import OVERRIDE_ON, AdvisorService

        service = AdvisorService(enabled=False)
        observer = service.attach("dashboard:a", override=OVERRIDE_ON)
        assert observer is not None

    def test_slot_off_override_refuses_despite_enabled_global(self):
        from kiro_crew.advisor.service import OVERRIDE_OFF, AdvisorService

        service = AdvisorService(enabled=True)
        assert service.attach("dashboard:a", override=OVERRIDE_OFF) is None
        assert service.observer_count() == 0

    def test_default_attach_keeps_inherit_semantics(self):
        from kiro_crew.advisor.service import AdvisorService

        assert AdvisorService(enabled=False).attach("dashboard:a") is None
        assert AdvisorService(enabled=True).attach("dashboard:a") is not None


class TestAdvisorConfigPatchSurface:
    """User customization: every advisor.* key is editable from the dashboard
    settings PATCH surface, and a patch re-applies to the live service."""

    def test_all_advisor_keys_are_patch_editable(self):
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        for key in (
            "advisor.enabled",
            "advisor.model",
            "advisor.non_blocker_budget",
            "advisor.cooldown_secs",
            "advisor.include_reasoning",
        ):
            assert key in _EDITABLE_CONFIG, f"{key} missing from the PATCH allowlist"

    def test_budget_and_cooldown_specs_are_bounded(self):
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        budget = _EDITABLE_CONFIG["advisor.non_blocker_budget"]
        assert budget["type"] == "int" and budget.get("min", -1) >= 0
        cooldown = _EDITABLE_CONFIG["advisor.cooldown_secs"]
        assert cooldown["type"] in ("int", "float") and cooldown.get("min", -1) >= 0


def _fresh():
    import kiro_crew.advisor.service as service_mod
    from kiro_crew.advisor.service import AdvisorService

    service_mod._service = AdvisorService(enabled=False)
    return service_mod._service


class TestPoolBindingFollowsEnablement:
    """Round-6: a disabled advisor constructs NOTHING at startup. The pool
    binds inside configure_from_config only when enabled, and unbinds (with
    a scheduled shutdown) when disabled -- so a settings toggle governs the
    whole lifecycle."""

    def test_disabled_config_binds_no_pool(self):
        service = _fresh()
        cfg = SimpleNamespace(advisor=SimpleNamespace(enabled=False, model=""))
        configure_from_config(cfg)
        assert getattr(service, "_pool", None) is None

    def test_enabled_config_binds_the_pool(self, monkeypatch):
        service = _fresh()
        built = {}

        def fake_build(model, work_dir=None):
            built["model"] = model
            return object()

        monkeypatch.setattr("kiro_crew.advisor.composition.build_reviewer_runtime", fake_build)
        cfg = SimpleNamespace(advisor=SimpleNamespace(enabled=True, model="rev-x"))
        configure_from_config(cfg)
        assert service._pool is not None
        assert built["model"] == "rev-x"

    def test_disabling_unbinds_the_pool(self, monkeypatch):
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: object(),
        )
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=True, model="")))
        assert service._pool is not None
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=False, model="")))
        assert getattr(service, "_pool", None) is None


class TestPoolReplacedOnModelChange:
    """Round-7 gpt: a live reviewer-model change must replace the pool, or
    cards keep getting labeled with the new model while the old runtime
    still serves them."""

    def test_model_change_rebuilds_the_pool(self, monkeypatch):
        service = _fresh()
        built = []

        def fake_build(model, work_dir=None):
            pool = SimpleNamespace(model=model, shut=False)

            async def shutdown():
                pool.shut = True

            pool.shutdown = shutdown
            built.append(pool)
            return pool

        monkeypatch.setattr("kiro_crew.advisor.composition.build_reviewer_runtime", fake_build)
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=True, model="A")))
        first = service._pool
        assert first.model == "A"
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=True, model="B")))
        assert service._pool is not first
        assert service._pool.model == "B"

    def test_same_model_keeps_the_pool(self, monkeypatch):
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: SimpleNamespace(model=model),
        )
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=True, model="A")))
        first = service._pool
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=True, model="A")))
        assert service._pool is first


class TestZeroIsAValue:
    """An operator-set 0 for budget or cooldown is a choice, not an absence."""

    def test_zero_budget_and_cooldown_survive_configure(self):
        service = _fresh()
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False, non_blocker_budget=0, cooldown_secs=0)
            )
        )
        assert service.non_blocker_budget == 0
        assert service.cooldown_secs == 0.0


class TestSessionOnUnderGlobalOff:
    """A session explicitly opted in must get the full advisor lifecycle even
    when the global default is off: a bound pool and processed boundaries."""

    def test_attach_binds_the_pool_when_effectively_enabled(self, monkeypatch):
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: SimpleNamespace(model=model),
        )
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=False, model="X")))
        assert service._pool is None  # global off constructs nothing eagerly
        observer = service.attach("dashboard:s1", override="on")
        assert observer is not None
        assert service._pool is not None and service._pool.model == "X"

    def test_boundary_processes_while_globally_disabled(self):
        service = _fresh()
        service._enabled = False
        observer = service.attach("dashboard:s2", override="on")
        assert observer is not None
        service.notify_boundary("dashboard:s2", "close")
        assert service._observers.get("dashboard:s2") is None


class TestDisableDetachesInheritedObservers:
    """A global disable must stop inherited observation NOW, not at the next
    attach -- checkpoints between disable and next turn would otherwise keep
    reaching the reviewer. Explicit per-session `on` survives by design."""

    def test_inherited_detached_explicit_on_kept(self):
        service = _fresh()
        service._enabled = True
        inherited = service.attach("dashboard:a", override="inherit")
        explicit = service.attach("dashboard:b", override="on")
        assert inherited is not None and explicit is not None
        configure_from_config(SimpleNamespace(advisor=SimpleNamespace(enabled=False, model="")))
        assert service._observers.get("dashboard:a") is None
        assert service._observers.get("dashboard:b") is explicit
