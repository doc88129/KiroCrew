"""Private member memory creation, ownership and fail-closed execution."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    config_dir,
    resolve_agent_bindings,
    update_config_locked,
)
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.memory_stores import (
    MEMBER_MEMORY_MANIFEST,
    MemberAlreadyExists,
    UnknownMemoryStore,
    memory_store_dir_for,
    memory_store_version,
    memory_stores_root,
    persist_member_config,
    provision_member_memory,
    require_member_memory_store,
    require_memory_store,
    resolve_store_path,
)


def _new_member(name: str = "reviewer") -> tuple[KiroCrewConfig, str]:
    cfg = KiroCrewConfig.load()
    cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    return cfg, provision_member_memory(cfg, name)


class TestPrivateOwnership:
    def test_partial_update_preserves_concurrent_fields_and_binding(self):
        cfg, store = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        stale = copy.deepcopy(cfg)
        stale.agents["reviewer"].kiro_agent = "new-template"

        def concurrent_edit(data):
            data["agents"]["reviewer"]["workspace"] = "concurrent-workspace"
            data["agents"]["reviewer"]["avatar"] = {"kind": "image", "v": 42}
            return data

        update_config_locked(mutate=concurrent_edit)
        persist_member_config(
            stale, "reviewer", expected_store=store, changed_fields={"kiro_agent"}
        )
        loaded = KiroCrewConfig.load()
        assert loaded.agents["reviewer"].kiro_agent == "new-template"
        assert loaded.agents["reviewer"].workspace == "concurrent-workspace"
        assert loaded.agents["reviewer"].avatar == {"kind": "image", "v": 42}
        assert require_member_memory_store(loaded, "reviewer") == store

    @pytest.mark.parametrize("occupied", [None, "invalid-entry", {}])
    def test_creation_refuses_every_occupied_member_key(self, occupied):
        cfg, _ = _new_member()

        def concurrent_creation(data):
            data.setdefault("agents", {})["reviewer"] = occupied
            return data

        update_config_locked(mutate=concurrent_creation)
        with pytest.raises(MemberAlreadyExists):
            persist_member_config(cfg, "reviewer", create=True)
        saved = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
        assert saved["agents"]["reviewer"] == occupied

    def test_partial_update_cannot_omit_a_new_binding_or_add_unknown_fields(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        cfg.save()
        provision_member_memory(cfg, "reviewer")
        with pytest.raises(UnknownMemoryStore, match="omitted its changed memory binding"):
            persist_member_config(
                cfg, "reviewer", expected_store="default", changed_fields={"workspace"}
            )
        with pytest.raises(UnknownMemoryStore, match="unknown fields"):
            persist_member_config(
                cfg, "reviewer", expected_store="default", changed_fields={"typo"}
            )
        assert KiroCrewConfig.load().agents["reviewer"].memory_store == "default"

    def test_member_starts_empty_and_global_files_are_unchanged(self):
        home = config_dir()
        (home / "memory.db").write_bytes(b"existing-v1-database")
        (home / "lessons.jsonl").write_text("global lessons", encoding="utf-8")
        cfg, store = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        root = memory_store_dir_for(store)
        assert {p.name for p in root.iterdir()} == {MEMBER_MEMORY_MANIFEST, "memory.db"}
        assert (home / "memory.db").read_bytes() == b"existing-v1-database"
        assert (home / "lessons.jsonl").read_text(encoding="utf-8") == "global lessons"
        loaded = KiroCrewConfig.load()
        assert loaded.default_agent == "default"
        assert resolve_agent_bindings(loaded, "default").memory_store_name == "default"
        assert resolve_agent_bindings(loaded, "reviewer").memory_store_name == store
        assert memory_store_version(store) == 2
        assert memory_store_version("default") == 1

    def test_members_have_distinct_stores_even_when_names_share_a_slug(self):
        cfg, first = _new_member("Code Review")
        cfg.agents["Code-Review"] = KiroCrewAgentConfig()
        second = provision_member_memory(cfg, "Code-Review")
        assert first != second
        assert require_member_memory_store(cfg, "Code Review") == first
        assert require_member_memory_store(cfg, "Code-Review") == second

    def test_selecting_member_as_default_does_not_grant_global_memory(self):
        cfg, store = _new_member()
        cfg.default_agent = "reviewer"
        assert resolve_agent_bindings(cfg).memory_store_name == store
        assert resolve_agent_bindings(cfg, "default").memory_store_name == "default"

    def test_store_listing_follows_exact_member_avatar_and_updates_without_rebinding(self):
        from kiro_crew.dashboard.handlers.memory_admin import _list_stores_blocking

        cfg, first = _new_member("Code Review")
        cfg.agents["Code Review"].avatar = {"kind": "image", "v": 11}
        persist_member_config(cfg, "Code Review", create=True)
        cfg = KiroCrewConfig.load()
        cfg.agents["Code-Review"] = KiroCrewAgentConfig(avatar={"kind": "image", "v": 22})
        second = provision_member_memory(cfg, "Code-Review")
        persist_member_config(cfg, "Code-Review", create=True)

        rows = {row["name"]: row for row in _list_stores_blocking()}
        assert rows[first]["owner_member"] == "Code Review"
        assert rows[first]["owner_avatar"] == {"kind": "image", "v": 11}
        assert rows[second]["owner_member"] == "Code-Review"
        assert rows[second]["owner_avatar"] == {"kind": "image", "v": 22}
        assert rows["default"]["owner_avatar"] == {}

        cfg = KiroCrewConfig.load()
        cfg.agents["Code Review"].avatar = {"kind": "image", "v": 33}
        persist_member_config(cfg, "Code Review", create=False, expected_store=first)
        refreshed = {row["name"]: row for row in _list_stores_blocking()}
        assert refreshed[first]["owner_avatar"] == {"kind": "image", "v": 33}
        assert refreshed[second]["owner_avatar"] == rows[second]["owner_avatar"]
        assert require_member_memory_store(KiroCrewConfig.load(), "Code Review") == first

    def test_mcp_advisory_binding_does_not_open_hidden_files_but_runtime_does(self):
        cfg, store = _new_member()
        (memory_stores_root() / store / "memory.db").unlink()
        assert (
            resolve_agent_bindings(cfg, "reviewer", validate_memory_files=False).memory_store_name
            == store
        )
        with pytest.raises(UnknownMemoryStore, match="database is missing or unreadable"):
            resolve_agent_bindings(cfg, "reviewer")

    @pytest.mark.parametrize("binding", ["default", "", "missing", "../escape", None])
    def test_legacy_or_broken_member_requires_deliberate_initialization(self, binding):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store=binding)
        with pytest.raises(UnknownMemoryStore, match="Initialize private memory"):
            resolve_agent_bindings(cfg, "reviewer")

    def test_shared_binding_is_refused_for_both_members(self):
        cfg, store = _new_member()
        cfg.agents["intruder"] = KiroCrewAgentConfig(memory_store=store)
        for name in ("reviewer", "intruder"):
            with pytest.raises(UnknownMemoryStore):
                require_member_memory_store(cfg, name)

    def test_missing_directory_is_not_recreated_on_resolution(self):
        cfg, store = _new_member()
        root = memory_stores_root() / store
        (root / MEMBER_MEMORY_MANIFEST).unlink()
        (root / "memory.db").unlink()
        root.rmdir()
        with pytest.raises(UnknownMemoryStore, match="missing or unreadable"):
            require_member_memory_store(cfg, "reviewer")
        assert not root.exists()

    def test_unreadable_directory_does_not_fall_back(self, monkeypatch):
        cfg, store = _new_member()
        import kiro_crew.memory_stores as stores

        def denied(_):
            raise PermissionError("access denied")

        monkeypatch.setattr(stores.os, "scandir", denied)
        with pytest.raises(UnknownMemoryStore, match="access denied"):
            require_memory_store(store, config=cfg)

    @pytest.mark.parametrize("missing", [True, False])
    def test_missing_or_invalid_database_is_not_recreated(self, missing):
        cfg, store = _new_member()
        database = memory_stores_root() / store / "memory.db"
        if missing:
            database.unlink()
        else:
            database.write_bytes(b"invalid database")
        with pytest.raises(UnknownMemoryStore, match="database is missing or unreadable"):
            require_member_memory_store(cfg, "reviewer")
        assert database.exists() is not missing

    def test_corrupt_or_wrong_ownership_manifest_refuses_execution(self):
        cfg, store = _new_member()
        manifest = memory_stores_root() / store / MEMBER_MEMORY_MANIFEST
        for payload in ("{bad", json.dumps({"owner_member": "someone-else", "memory_version": 2})):
            manifest.write_text(payload, encoding="utf-8")
            with pytest.raises(UnknownMemoryStore):
                require_member_memory_store(cfg, "reviewer")

    def test_store_link_to_another_member_is_refused(self):
        cfg, store = _new_member()
        cfg.agents["other"] = KiroCrewAgentConfig()
        other = provision_member_memory(cfg, "other")
        root = memory_stores_root() / store
        (root / MEMBER_MEMORY_MANIFEST).unlink()
        (root / "memory.db").unlink()
        root.rmdir()
        make_dir_link(root, memory_stores_root() / other)
        with pytest.raises(UnknownMemoryStore, match="refusing a link"):
            require_member_memory_store(cfg, "reviewer")

    def test_undeclared_store_never_resolves_to_configured_or_global_default(self):
        cfg = KiroCrewConfig.load()
        cfg.memory_stores["legacy"] = MemoryStoreConfig()
        cfg.default_memory_store = "legacy"
        cfg.save()
        with pytest.raises(UnknownMemoryStore, match="not declared"):
            resolve_store_path("missing")

    def test_existing_private_store_cannot_be_reset_by_provision(self):
        cfg, store = _new_member()
        assert provision_member_memory(cfg, "reviewer") == store
        assert len([v for v in cfg.memory_stores.values() if v.owner_member == "reviewer"]) == 1

    def test_legacy_named_store_is_not_adopted_or_copied(self):
        cfg = KiroCrewConfig.load()
        cfg.memory_stores["legacy"] = MemoryStoreConfig()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store="legacy")
        cfg.save()
        legacy = memory_store_dir_for("legacy")
        legacy.mkdir(parents=True)
        (legacy / "lessons.jsonl").write_text("legacy content", encoding="utf-8")
        store = provision_member_memory(cfg, "reviewer")
        assert store != "legacy"
        assert (legacy / "lessons.jsonl").read_text(encoding="utf-8") == "legacy content"
        assert not (memory_stores_root() / store / "lessons.jsonl").exists()
        assert memory_store_version("legacy") == 1

    def test_concurrent_creation_has_one_winner_and_preserves_other_settings(self):
        cfg = KiroCrewConfig.load()
        cfg.save()
        first, _ = _new_member()
        second, _ = _new_member()

        def publish(snapshot):
            try:
                persist_member_config(snapshot, "reviewer", create=True)
                return True
            except UnknownMemoryStore:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, [first, second]))
        assert sorted(results) == [False, True]
        loaded = KiroCrewConfig.load()
        assert len([v for v in loaded.memory_stores.values() if v.owner_member == "reviewer"]) == 1
        require_member_memory_store(loaded, "reviewer")

    def test_metadata_edit_does_not_resurrect_a_concurrently_removed_member(self):
        cfg, store = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        stale_editor = KiroCrewConfig.load()
        stale_editor.agents["reviewer"].description = "Unsaved edit"
        latest = KiroCrewConfig.load()
        del latest.agents["reviewer"]
        latest.save()
        with pytest.raises(UnknownMemoryStore, match="removed concurrently"):
            persist_member_config(stale_editor, "reviewer", expected_store=store)
        assert "reviewer" not in KiroCrewConfig.load().agents


@pytest.fixture
def owner_crud_app(monkeypatch):
    import kiro_crew.dashboard.handlers.agents as handlers

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    monkeypatch.setattr(handlers, "list_agents", lambda: [])
    app = web.Application()
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", handlers.api_kirocrew_agent_update)
    return app


class TestMemberMemoryUserFlows:
    @pytest.mark.asyncio
    async def test_create_edit_and_refuse_rebinding(self, owner_crud_app):
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert response.status == 200, await response.text()
            store = (await response.json())["memory_store"]
            response = await client.put(
                "/api/agents/reviewer",
                json={"description": "Checks patches", "memory_store": store},
            )
            assert response.status == 200, await response.text()
            response = await client.put("/api/agents/reviewer", json={"memory_store": "default"})
            assert response.status == 409
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        assert loaded.agents["reviewer"].description == "Checks patches"
        assert loaded.agents["reviewer"].memory_store == store

    @pytest.mark.asyncio
    async def test_legacy_member_metadata_edits_do_not_initialize_memory(self, owner_crud_app):
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        await asyncio.to_thread(cfg.save)
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.put(
                "/api/agents/reviewer", json={"description": "Checks patches"}
            )
            assert response.status == 200
            unchanged = await asyncio.to_thread(KiroCrewConfig.load)
            assert unchanged.agents["reviewer"].memory_store == "default"
            with pytest.raises(UnknownMemoryStore):
                await asyncio.to_thread(require_member_memory_store, unchanged, "reviewer")
            response = await client.put("/api/agents/reviewer", json={"provision_memory": True})
            assert response.status == 200, await response.text()
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        await asyncio.to_thread(require_member_memory_store, loaded, "reviewer")

    def test_cli_creates_private_memory_and_refuses_rebinding(self, capsys):
        from kiro_crew.cli_commands import _handle_agent

        _handle_agent(
            argparse.Namespace(
                agent_action="create",
                name="reviewer",
                kiro_agent="kirocrew",
                workspace="default",
                memory_store="default",
            )
        )
        loaded = KiroCrewConfig.load()
        require_member_memory_store(loaded, "reviewer")
        with pytest.raises(SystemExit) as exc:
            _handle_agent(
                argparse.Namespace(
                    agent_action="update",
                    name="reviewer",
                    kiro_agent=None,
                    workspace=None,
                    memory_store="default",
                )
            )
        assert exc.value.code == 1
        assert "cannot be rebound" in capsys.readouterr().err
