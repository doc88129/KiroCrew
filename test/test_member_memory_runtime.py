"""Private member identity survives scheduling, retries and process restarts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.cron import CronJob, CronService, resolve_cron_memory
from kiro_crew.history import ConversationLog
from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import (
    _run_memory_identity_path,
    create_agent_folder,
    read_run_memory_store,
)

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def member_stores(monkeypatch):
    # Provider calls in this file are doubles; model the supported WSL runtime.
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_execution_supported", lambda **kwargs: True
    )
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="write")
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="review")
    writer = provision_member_memory(cfg, "writer")
    reviewer = provision_member_memory(cfg, "reviewer")
    cfg.save()
    return writer, reviewer


def test_restarted_run_restores_protected_identity_not_agent_editable_state(member_stores):
    writer, reviewer = member_stores
    folder = create_agent_folder("run1", memory_store=writer)
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    state["memory_store"] = reviewer
    (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert not manager._agents
    assert manager._inherited_memory_store("run1") == writer


def test_missing_private_resume_record_fails_instead_of_global(member_stores):
    writer, _ = member_stores
    create_agent_folder("run1", memory_store=writer)
    _run_memory_identity_path("run1").unlink()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    with patch.object(manager, "spawn") as spawn:
        result = manager.continue_conversation("run1", "continue")
    assert result.done and result.error.startswith("memory_unavailable:")
    spawn.assert_not_called()


def test_legacy_resume_without_member_binding_remains_global():
    assert read_run_memory_store("legacy") == ""


@pytest.mark.parametrize("replacement", [None, "", "default", "other"])
def test_private_channel_binding_survives_restart_and_refuses_metadata_downgrade(
    member_stores, replacement
):
    from kiro_crew.context import store_of_session
    from kiro_crew.member_memory_auth import bind_private_session_store

    writer, reviewer = member_stores
    key = "slack:private-channel-thread"
    log = ConversationLog()
    log.init()
    log.update_metadata(key, {"memory_store": writer})
    bind_private_session_store(key, writer)
    assert store_of_session(ConversationLog(), key) == writer
    record = (
        {}
        if replacement is None
        else {"memory_store": reviewer if replacement == "other" else replacement}
    )
    with pytest.raises(UnknownMemoryStore, match="protected member binding"):
        store_of_session(SimpleNamespace(get_metadata=lambda _: record), key)
    assert store_of_session(SimpleNamespace(get_metadata=lambda _: {}), "slack:legacy") == ""


def test_private_session_cannot_be_rebound_or_lose_its_protected_record(member_stores):
    from kiro_crew.context import store_of_session
    from kiro_crew.member_memory_auth import _session_binding_path, bind_private_session_store

    writer, reviewer = member_stores
    key = "dashboard:pinned-writer"
    bind_private_session_store(key, writer)
    with pytest.raises(ValueError, match="already bound"):
        bind_private_session_store(key, reviewer)
    _session_binding_path(key).unlink()
    with pytest.raises(UnknownMemoryStore, match="protected member session binding"):
        store_of_session(SimpleNamespace(get_metadata=lambda _: {}), key)


def test_provider_factory_derives_private_fence_from_persisted_identity(member_stores):
    writer, _ = member_stores
    log = ConversationLog()
    log.init()
    log.update_metadata("dashboard:member-writer", {"memory_store": writer})
    create_agent_folder("private-worker", memory_store=writer)
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        factory = KiroCrewConfig.load().create_provider_factory()
        factory("dashboard:member-writer")
        assert provider.call_args.kwargs["private_memory"] is True
        factory("subagent:private-worker")
        assert provider.call_args.kwargs["private_memory"] is True
        factory("dashboard:unowned", private_memory=True)
        assert "private_memory" not in provider.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_provider_preserves_private_fence_when_constructing_runtime(tmp_path, private):
    from kiro_crew.providers.acp import AcpProvider

    with patch("kiro_crew.providers.acp.AcpClient") as client_type:
        provider = AcpProvider(private_memory=private)
    assert client_type.call_args.kwargs.get("private_memory", False) is private
    provider._client = SimpleNamespace(
        _work_dir=tmp_path,
        _agent="kirocrew",
        _sandbox_mode="auto",
        _extra_env={},
        _mcp_gateway_overlay=None,
        _mcp_gateway_socket=None,
        _private_mcp_gateway_socket=str(tmp_path / "custom-broker.sock"),
        _resume_session_id="",
        _model="auto",
        backend="",
    )
    runtime = MagicMock(spawn=AsyncMock(side_effect=RuntimeError("stop before process launch")))
    with patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime) as runtime_type:
        with pytest.raises(RuntimeError, match="stop before process launch"):
            await provider._start_kiro_runtime_impl({}, {})
    assert runtime_type.call_args.kwargs.get("private_memory", False) is private
    assert runtime_type.call_args.kwargs["mcp_gateway_socket"] == (
        str(tmp_path / "custom-broker.sock") if private else None
    )


@pytest.mark.asyncio
async def test_private_consolidation_uses_separate_bound_process_and_cleans_it_up(member_stores):
    from kiro_crew.llm_helpers import background_turn
    from kiro_crew.member_memory_auth import private_memory_store_for_session
    from kiro_crew.session import BACKGROUND_KEY

    client = SimpleNamespace(last_prompt_stats=None)
    sessions = MagicMock(
        get_or_create=AsyncMock(return_value=(client, True, False)),
        remove=AsyncMock(),
        recycle_background=AsyncMock(),
    )
    keys = []
    for store in member_stores:
        async with background_turn(
            sessions, task="consolidation", agent="kirocrew-lite", memory_store=store
        ):
            key = sessions.get_or_create.call_args.args[0]
            keys.append(key)
            assert key != BACKGROUND_KEY
            assert private_memory_store_for_session(key) == store
        sessions.release.assert_called_with(key)
        sessions.remove.assert_awaited_with(key)
    assert len(set(keys)) == 2
    sessions.recycle_background.assert_not_awaited()


@pytest.mark.parametrize("contents", ["{truncated", "[]", "null"])
def test_unreadable_run_without_protected_identity_cannot_become_global(contents):
    from kiro_crew import subagent_persistence as persistence

    folder = persistence._agent_dir("broken-identity")
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="run metadata is unreadable"):
        read_run_memory_store("broken-identity")


def test_unprotected_old_record_cannot_authorize_a_private_resume(member_stores):
    from kiro_crew import subagent_persistence as persistence

    writer, _ = member_stores
    old = persistence._cleanup_identities_path("old-identity").parent / "memory.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"version": 2, "memory_store": writer}), encoding="utf-8")
    with pytest.raises(ValueError, match="protected memory record"):
        read_run_memory_store("old-identity")


def test_missing_private_record_cannot_be_hidden_by_editing_run_state(member_stores):
    writer, _ = member_stores
    folder = create_agent_folder("edited-state", memory_store=writer)
    _run_memory_identity_path("edited-state").unlink()
    (folder / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="protected memory record"):
        read_run_memory_store("edited-state")


def test_schedule_member_survives_reload_without_origin_chat(tmp_path, member_stores):
    writer, _ = member_stores
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "write report", every_secs=60, member_id="writer")
    reloaded = CronService(base_dir=tmp_path / "cron").get_job(job.id)
    assert reloaded.member_id == "writer"
    assert reloaded.memory_store == writer
    assert resolve_cron_memory(reloaded) == (writer, "kirocrew")


@pytest.mark.parametrize("mode", ["command", "script"])
@pytest.mark.parametrize("inherit", [False, True])
def test_private_deterministic_schedule_refused_before_persistence(
    tmp_path, member_stores, mode, inherit
):
    writer, _ = member_stores
    ConversationLog().update_metadata("dashboard:writer", {"memory_store": writer})
    identity = {"session_key": "dashboard:writer"} if inherit else {"member_id": "writer"}
    body = "echo hello" if mode == "command" else "report.py:run"
    service = CronService(base_dir=tmp_path / "cron")
    with pytest.raises(ValueError, match="require an agent task"):
        service.add_job("daily", "", every_secs=60, **identity, **{mode: body})
    assert service.list_jobs() == []
    assert CronService(base_dir=tmp_path / "cron").list_jobs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["command", "script"])
async def test_imported_private_deterministic_schedule_never_dispatches(member_stores, mode):
    from test_cron_gateway_integration import _make_gw_for_llm, _run_llm_callback

    writer, _ = member_stores
    gateway = _make_gw_for_llm()
    job = CronJob(
        id="private-import", name="daily", message="", member_id="writer", memory_store=writer
    )
    setattr(job, mode, "echo hello" if mode == "command" else "report.py:run")
    with (
        patch("kiro_crew.slack.gateway.run_command_sandboxed") as command,
        patch("kiro_crew.slack.gateway.run_script_sandboxed") as script,
        pytest.raises(ValueError, match="require an agent task"),
    ):
        await _run_llm_callback(gateway, job)
    command.assert_not_called()
    script.assert_not_called()
    gateway.sessions.get_or_create.assert_not_called()


@pytest.mark.parametrize("bad_identity", [None, False, 0, [], {}])
@pytest.mark.parametrize("field", ["member_id", "memory_store"])
def test_malformed_schedule_identity_never_means_global(field, bad_identity):
    job = CronJob(id="damaged", name="damaged", message="task")
    setattr(job, field, bad_identity)
    with pytest.raises(ValueError, match="memory identity is malformed"):
        resolve_cron_memory(job)


@pytest.mark.parametrize("bad_identity", [None, False, 0, [], {}])
def test_malformed_spawn_identity_is_refused_before_queueing(bad_identity):
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    with patch("kiro_crew.subagent.check_memory_available") as host_check:
        result = manager.spawn("task", memory_store=bad_identity)
    assert result.done and result.error.startswith("memory_unavailable:")
    host_check.assert_not_called()
    assert not manager._agents


@pytest.mark.asyncio
async def test_native_windows_private_execution_refuses_before_preparing_provider(
    member_stores, monkeypatch
):
    from kiro_crew.context import prepare_store_vectors

    writer, _ = member_stores
    monkeypatch.setattr("kiro_crew.member_memory_auth.sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_execution_supported", lambda **kwargs: False
    )
    builder = MagicMock(ensure_store=AsyncMock())
    with pytest.raises(UnknownMemoryStore, match="WSL/Linux gateway"):
        await prepare_store_vectors(builder, writer)
    builder.ensure_store.assert_not_called()
    await prepare_store_vectors(builder, "default")
    builder.ensure_store.assert_not_called()


def test_schedule_inherits_creator_member_once(tmp_path, member_stores):
    writer, reviewer = member_stores
    log = ConversationLog()
    log.update_metadata("dashboard:writer", {"memory_store": writer, "agent": "writer"})
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "write report", every_secs=60, session_key="dashboard:writer")
    log.update_metadata("dashboard:writer", {"memory_store": reviewer, "agent": "reviewer"})
    assert job.member_id == "writer"
    assert resolve_cron_memory(job)[0] == writer


def test_v1_provider_template_does_not_become_a_member(tmp_path, member_stores):
    job = CronService(base_dir=tmp_path / "cron").add_job(
        "legacy", "task", every_secs=60, agent_id="writer"
    )
    assert job.member_id == job.memory_store == ""
    assert resolve_cron_memory(job) == ("", "writer")


def test_sandbox_crew_selection_does_not_open_private_memory(member_stores):
    from kiro_crew.mcp_core import _do_select_crew

    writer, _ = member_stores
    with patch(
        "kiro_crew.memory_stores._named_store_dir", side_effect=PermissionError("sandbox hidden")
    ):
        selected = json.loads(_do_select_crew("writer"))
    assert selected["bound"]["memory_store"] == writer


@pytest.mark.parametrize("origin", ["dashboard:writer", "subagent:writer-run"])
def test_sandbox_schedule_inherits_binding_without_opening_private_memory(
    tmp_path, member_stores, origin
):
    writer, _ = member_stores
    if origin.startswith("subagent:"):
        create_agent_folder("writer-run", memory_store=writer)
    else:
        ConversationLog().update_metadata(origin, {"memory_store": writer})
    with patch(
        "kiro_crew.memory_stores._named_store_dir", side_effect=PermissionError("sandbox hidden")
    ):
        job = CronService(base_dir=tmp_path / "cron").add_job(
            "scheduled", "write", every_secs=60, session_key=origin
        )
    assert job.member_id == "writer"
    assert job.memory_store == writer
    assert resolve_cron_memory(job) == (writer, "kirocrew")


def test_corrupt_existing_transcript_refuses_memory_resolution_and_scheduling(tmp_path):
    from kiro_crew.context import store_of_session

    log = ConversationLog()
    log.update_metadata("dashboard:broken", {"memory_store": "private-identity"})
    log._path("dashboard:broken").write_text("{truncated metadata\n", encoding="utf-8")
    with pytest.raises(UnknownMemoryStore, match="global memory was not used"):
        store_of_session(log, "dashboard:broken")
    service = CronService(base_dir=tmp_path / "cron")
    with pytest.raises(ValueError, match="unreadable"):
        service.add_job("scheduled", "task", every_secs=60, session_key="dashboard:broken")
    assert service.list_jobs() == []


def test_schedule_refuses_rebinding_without_partial_changes(tmp_path, member_stores):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, member_id="writer")
    with pytest.raises(ValueError, match="fixed"):
        service.update_job(job.id, name="changed", member_id="reviewer")
    stored = service.get_job(job.id)
    assert stored.name == "daily"
    assert stored.member_id == "writer"


@pytest.mark.parametrize("recorded_agent", ["writer", "", "default", "custom-template"])
def test_linked_member_hydrates_provider_template_and_keeps_its_memory(
    member_stores, recorded_agent, monkeypatch
):
    from kiro_crew.context import store_of_session
    from kiro_crew.messaging.session_resume import persisted_session_agent
    from kiro_crew.slack import handler

    # Hydration's seen set and result map form one cache. Isolate both so a
    # preceding parameter on the same xdist worker cannot skip this hydration.
    monkeypatch.setattr(handler, "_hydrated_sessions", set())
    monkeypatch.setattr(handler, "_thread_agents", {})
    writer, _ = member_stores
    log = ConversationLog()
    key = "dashboard:linked-writer"
    log.update_metadata(key, {"memory_store": writer, "agent": recorded_agent})
    expected = "custom-template" if recorded_agent == "custom-template" else "kirocrew"
    assert persisted_session_agent(log, key) == expected
    handler._hydrate_thread_overrides(key, log)
    assert handler._thread_agents[key] == expected
    assert store_of_session(log, key) == writer


def test_unowned_v1_agent_name_is_not_reinterpreted_as_member(member_stores):
    from kiro_crew.messaging.session_resume import persisted_session_agent

    log = ConversationLog()
    log.update_metadata("slack:legacy", {"agent": "writer"})
    assert persisted_session_agent(log, "slack:legacy") == "writer"


@pytest.mark.asyncio
async def test_unavailable_private_run_refused_before_provider_allocation():
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock()
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    info = SubagentInfo(id="run1", task="task", memory_store="missing-private")
    with pytest.raises(UnknownMemoryStore):
        await asyncio.wait_for(manager._run_inner(info, "subagent:run1"), 5)
    sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_linked_channel_refuses_private_memory_before_provider_and_displays_reason(
    monkeypatch,
):
    from test_messaging_dispatch import _CtxBuilder, _patch_pipeline, _Sessions, _turn

    from kiro_crew.messaging.dispatch import drive_turn

    _patch_pipeline(monkeypatch)
    sessions = _Sessions()
    sessions.get_or_create = AsyncMock()
    renderer = MagicMock()
    renderer.on_turn_start = AsyncMock()
    renderer.on_text_chunk = AsyncMock()
    renderer.on_done = AsyncMock()
    renderer.close = AsyncMock()
    turn = _turn(renderer)
    builder = _CtxBuilder()
    builder.conversation_log = ConversationLog()
    builder.conversation_log.update_metadata(turn.session_key, {"memory_store": "missing-private"})

    await drive_turn(turn, sessions=sessions, ctx_builder=builder)

    sessions.get_or_create.assert_not_called()
    renderer.on_text_chunk.assert_awaited_once()
    refusal = renderer.on_text_chunk.call_args.args[0]
    assert "missing-private" in refusal and "not declared" in refusal
    renderer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_binding_publication_blocks_an_already_scheduled_run(member_stores):
    writer, _ = member_stores
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock()
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    info = SubagentInfo(id="run1", task="task", memory_store=writer)
    with patch("kiro_crew.subagent.create_agent_folder", side_effect=OSError("disk full")):
        manager._log_spawned(info)
    assert info.error.startswith("memory_unavailable:")
    with pytest.raises(RuntimeError, match="could not persist"):
        await asyncio.wait_for(manager._run_inner(info, "subagent:run1"), 5)
    sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_crew_retry_keeps_delegate_memory_and_template(tmp_path, monkeypatch):
    from test_crew_chat import _orch, _slot, _spawn_info

    import kiro_crew.crew_chat as crew_mod

    monkeypatch.setattr(crew_mod, "data_home", lambda: tmp_path)
    subagents = MagicMock()
    subagents.continue_conversation.return_value = _spawn_info(
        "expired", done=True, error="conversation_gone: expired"
    )
    subagents.spawn.return_value = _spawn_info("retry")
    orchestrator = _orch(subagents=subagents)
    slot = _slot()
    slot.memory_store = "parent-store"
    store = orchestrator._store("s1")
    topic = store.add_topic("original", "original", "write", "first")
    topic.update(status="idle", memory_store="delegate-store", dispatch_agent="delegate-template")
    entry = store.add_msg("continue")
    await orchestrator._dispatch_continue(slot, store, topic, entry)
    assert subagents.spawn.call_args.kwargs["memory_store"] == "delegate-store"
    assert subagents.spawn.call_args.kwargs["agent"] == "delegate-template"
    await crew_mod.CrewStore.wait_for(store.save())


@pytest.mark.asyncio
async def test_scheduled_member_passes_private_store_to_context(member_stores):
    from test_cron_gateway_integration import _make_gw_for_llm, _run_llm_callback

    writer, _ = member_stores
    gateway = _make_gw_for_llm()
    gateway.ctx_builder.conversation_log = ConversationLog()
    gateway.ctx_builder.ensure_store = AsyncMock()
    job = CronJob(
        id="member-job", name="daily", message="report", member_id="writer", memory_store=writer
    )
    seen = []

    async def acquire(*args, **kwargs):
        seen.append(gateway.ctx_builder.conversation_log.get_metadata(args[0])["memory_store"])
        return MagicMock(), True, False

    with patch("kiro_crew.context.prepare_store_vectors", new=AsyncMock()) as prepare:
        await _run_llm_callback(gateway, job, get_or_create_side_effect=acquire)
    assert seen == [writer]
    prepare.assert_awaited_once_with(
        gateway.ctx_builder, writer, session_key=gateway.sessions.get_or_create.call_args.args[0]
    )
    assert gateway.sessions.get_or_create.call_args.kwargs["agent"] == "kirocrew"


@pytest.mark.asyncio
async def test_deleted_scheduled_member_never_starts_global_provider():
    from test_cron_gateway_integration import _make_gw_for_llm, _run_llm_callback

    gateway = _make_gw_for_llm()
    job = CronJob(id="member-job", name="daily", message="report", member_id="deleted")
    with pytest.raises(ValueError, match="unknown Crew Member"):
        await _run_llm_callback(gateway, job)
    gateway.sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_failed_pending_restore_aborts_startup_before_memory_opens():
    from test_slack_gateway import _make_orchestrator

    from kiro_crew.memory_backup import MemoryBackupFailed

    gateway = _make_orchestrator()
    gateway._warn_if_kiro_cli_outdated = AsyncMock()
    gateway._check_missing_deps = AsyncMock()
    with (
        patch("kiro_crew.agent.rebuild_agent_config"),
        patch("kiro_crew.agent.missing_required_agent_specs", return_value=[]),
        patch("kiro_crew.slack.gateway.safe_context_call"),
        patch("kiro_crew.slack.gateway.build_provider_factory"),
        patch(
            "kiro_crew.memory_backup.apply_pending_member_restores",
            side_effect=MemoryBackupFailed("writer restore failed; previous data preserved"),
        ),
        patch("kiro_crew.slack.gateway.MemoryStore") as memory,
        patch("kiro_crew.vector_memory.VectorMemoryStore") as vectors,
    ):
        with pytest.raises(MemoryBackupFailed, match="previous data preserved"):
            await asyncio.wait_for(gateway._init_services(), 5)
    memory.assert_not_called()
    vectors.assert_not_called()
