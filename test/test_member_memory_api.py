"""Member recall and explicit seed cross the real owner/session/store gates."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_core, memory_schema, memory_stores
from kiro_crew.config import loader
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import cron, memory, memory_admin, memory_member
from kiro_crew.mcp_tools import learn
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group("member_memory_api")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = {
        "memory_stores": {"default": {}},
        "agents": {},
    }
    tiers = {}
    for member in ("alice", "bob"):
        name = f"member-{member}"
        directory = tmp_path / "memory_stores" / name
        directory.mkdir(parents=True)
        record = {"memory_version": 2, "owner_member": member}
        (directory / "member-memory.json").write_text(json.dumps(record), encoding="utf-8")
        config["memory_stores"][name] = record
        config["agents"][member] = {"memory_store": name}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
    for name in ("", "member-alice", "member-bob"):
        path = (
            tmp_path / "memory.db" if not name else tmp_path / "memory_stores" / name / "memory.db"
        )
        tier = VectorMemoryStore(db_path=path)
        tier.init()
        tiers[name] = tier
    metadata = {"dashboard:alice": {"memory_store": "member-alice"}}
    state = SimpleNamespace(
        owner_id="owner",
        context_builder=SimpleNamespace(memory=SimpleNamespace(vector_store=tiers[""])),
        conversation_log=SimpleNamespace(get_metadata=lambda key: metadata.get(key, {})),
        sessions=None,
        consolidator=None,
        _restricted_keys=set(),
        _slots={"alice": SimpleNamespace(is_restricted=False, blocks_reads=False)},
    )

    async def ensure(name):
        memory_stores.require_memory_store(name)
        return tiers.get(name)

    monkeypatch.setattr(ContextBuilder, "ensure_store", staticmethod(ensure))
    monkeypatch.setattr(cron, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kwargs: None))
    try:
        yield SimpleNamespace(tiers=tiers, state=state, metadata=metadata, home=tmp_path)
    finally:
        for tier in tiers.values():
            tier.close()
        loader._invalidate_config_cache()


def request(
    env, *, body=None, query=None, owner=False, internal=False, session="dashboard:alice", proof=""
):
    method = "POST" if body is not None else "GET"
    target = "/api/memory/seed" if body is not None else "/api/memory/recall"
    if query:
        target += "?" + urlencode(query)
    app = web.Application()
    app["state"] = env.state
    headers = {"X-Session-Key": session}
    if proof:
        headers["X-Member-Session-Proof"] = proof
    kwargs = {}
    if body is not None:
        raw = json.dumps(body).encode()
        reader = streams.StreamReader(
            mock.Mock(_reading_paused=False), limit=len(raw) + 1, loop=asyncio.get_running_loop()
        )
        reader.feed_data(raw)
        reader.feed_eof()
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        kwargs["payload"] = reader
    result = make_mocked_request(method, target, app=app, headers=headers, **kwargs)
    if owner:
        result["user"] = "owner"
        result["app"] = ""
    if internal:
        result["internal_auth"] = True
    return result


@pytest.fixture
def member_proof(env, monkeypatch):
    from kiro_crew import member_memory_auth, platform_compat

    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"test-start-{pid}")
    member_memory_auth.publish_member_session_pid(
        os.getpid(), "dashboard:alice", memory_store="member-alice"
    )
    proof = member_memory_auth.issue_member_session_proof("dashboard:alice", os.getpid())
    assert proof
    return proof


def seed_body(*items, source="default", target="member-alice"):
    return {"source_store": source, "store": target, "items": list(items)}


@pytest.mark.asyncio
async def test_owner_can_stage_restore_of_lost_member_directory_and_see_pending_refusal(env):
    from kiro_crew import memory_backup

    tier = env.tiers["member-alice"]
    tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    directory = env.home / "memory_stores" / "member-alice"
    backup = memory_backup.backup_store(directory / "memory.db")
    tier.close()
    directory.rename(env.home / "lost-alice")
    listing = await memory_admin.api_memory_backups(
        request(env, query={"store": "member-alice"}, owner=True)
    )
    assert listing.status == 200
    assert json.loads(listing.text)["backups"][0]["name"] == backup.name
    body = {"store": "member-alice", "name": backup.name}
    response = await memory_admin.api_memory_restore(request(env, body=body, owner=True))
    assert response.status == 200
    assert json.loads(response.text)["pending"] is True
    assert json.loads(response.text)["restart_required"] is True
    assert not directory.exists()
    refused = await memory_admin.api_memory_restore(request(env, body=body, owner=True))
    assert refused.status == 409
    assert json.loads(refused.text)["code"] == "restore_refused"
    assert "already pending" in json.loads(refused.text)["error"]
    with pytest.raises(memory_stores.UnknownMemoryStore):
        memory_stores.require_memory_store("member-alice")
    assert memory_backup.apply_pending_member_restores() == {"member-alice": ""}
    restored = VectorMemoryStore(db_path=directory / "memory.db")
    restored.init()
    env.tiers["member-alice"] = restored
    assert json.loads(restored.get_semantic("project.database")["value_json"]) == "PostgreSQL"


@pytest.mark.asyncio
async def test_selected_owner_seed_is_traceable_idempotent_and_independent(env):
    global_store = env.tiers[""]
    global_store.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    global_store.set_semantic("user.private", "Do not copy this detail", 1.0, "user_explicit")
    body = seed_body({"kind": "fact", "id": "project.database"})
    response = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert response.status == 200
    assert json.loads(response.text)["results"][0]["outcome"] == "imported"
    alice = env.tiers["member-alice"]
    assert alice.get_semantic("user.private") is None
    assert env.tiers["member-bob"].get_all_semantic() == []
    row = alice.list_by_facets(kind="fact")[0]
    assert json.loads(row["derived_from"])["store"] == "default"
    response = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert json.loads(response.text)["results"][0]["outcome"] == "existing"
    alice.delete_semantic("project.database", "user_explicit")
    assert global_store.get_semantic("project.database") is not None


@pytest.mark.asyncio
async def test_paginated_lists_keep_fact_and_episode_copy_provenance_after_reopen(env):
    source = env.tiers["member-bob"]
    source.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    assert source.write_episodic(
        "Reviewed PostgreSQL database migration",
        conversation_id="database design",
        tags=["database"],
        importance=0.8,
        defer_embedding=True,
    )
    episode = source.get_episodic_list()[0]["id"]
    selections = [
        {"kind": "fact", "id": "project.database"},
        {"kind": "episode", "id": episode},
    ]
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body(*selections, source="member-bob"), owner=True)
    )
    assert response.status == 200
    assert all(item["outcome"] == "imported" for item in json.loads(response.text)["results"])
    old_tier = env.tiers["member-alice"]
    old_tier.close()
    tier = VectorMemoryStore(db_path=env.home / "memory_stores" / "member-alice" / "memory.db")
    tier.init()
    env.tiers["member-alice"] = tier
    for handler, expected in (
        (memory.api_memory_semantic, selections[0]),
        (memory.api_memory_episodic_list, selections[1]),
    ):
        response = await handler(
            request(env, query={"store": "member-alice", "limit": "1", "offset": "0"}, owner=True)
        )
        assert response.status == 200
        entries = json.loads(response.text)["entries"]
        assert len(entries) == 1
        assert entries[0]["source"] == "user_seed"
        lineage = json.loads(entries[0]["derived_from"])
        assert lineage["store"] == "member-bob"
        assert lineage["item_id"] == expected["id"]
        assert lineage["kind"] == expected["kind"]
        assert lineage["copied_at"]
        response = await handler(
            request(env, query={"store": "member-alice", "limit": "1", "offset": "1"}, owner=True)
        )
        assert json.loads(response.text)["entries"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_seed_cannot_be_authorized_by_agent_or_header(env, internal):
    env.tiers[""].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": "fact", "id": "project.database"}), internal=internal)
    )
    assert response.status == 403
    assert env.tiers["member-alice"].get_all_semantic() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,target",
    [
        ("absent", "member-alice"),
        ("default", "absent"),
        ("member-bob", "default"),
        ("member-alice", "member-alice"),
    ],
)
async def test_invalid_source_or_destination_never_writes(env, source, target):
    response = await memory_member.api_memory_seed(
        request(
            env,
            body=seed_body(
                {"kind": "fact", "id": "project.database"}, source=source, target=target
            ),
            owner=True,
        )
    )
    assert response.status in {400, 404, 409}
    assert all(t.get_all_semantic() == [] for t in env.tiers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last",
    [
        {"kind": "fact", "id": "missing"},
        {"kind": "directive", "id": "project.database"},
        {"kind": "fact", "id": "project.database"},
    ],
)
async def test_stale_or_invalid_selection_is_checked_before_any_copy(env, last):
    env.tiers[""].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": "fact", "id": "project.database"}, last), owner=True)
    )
    assert response.status in {400, 409}
    assert env.tiers["member-alice"].get_all_semantic() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [[], {}])
async def test_unhashable_seed_kind_is_a_validation_error_without_writes(env, kind):
    env.tiers[""].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": kind, "id": "project.database"}), owner=True)
    )
    assert response.status == 400
    assert json.loads(response.text)["code"] == "invalid_seed_items"
    assert env.tiers["member-alice"].get_all_semantic() == []
    assert env.tiers["member-bob"].get_all_semantic() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_before_error", [False, True])
async def test_seed_returns_truthful_partial_results_if_a_later_copy_fails(
    env, monkeypatch, commit_before_error
):
    source, destination = env.tiers[""], env.tiers["member-alice"]
    keys = ["project.database", "project.language", "project.region"]
    for key, value in zip(keys, ["PostgreSQL", "Python", "us-east"]):
        source.set_semantic(key, value, 1.0, "user_explicit")
    original = destination.seed_item_if_absent

    def fail_second(item, **kwargs):
        if kwargs["source_id"] == keys[1]:
            if commit_before_error:
                original(item, **kwargs)
            raise OSError("disk unavailable")
        return original(item, **kwargs)

    monkeypatch.setattr(destination, "seed_item_if_absent", fail_second)
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body(*[{"kind": "fact", "id": key} for key in keys]), owner=True)
    )
    assert response.status == 200
    data = json.loads(response.text)
    assert data["partial"] is True
    assert [item["outcome"] for item in data["results"]] == [
        "imported",
        "unconfirmed",
        "not_attempted",
    ]
    assert [item["source_id"] for item in data["results"]] == keys
    assert "disk unavailable" in data["results"][1]["reason"]
    assert destination.get_semantic(keys[0]) is not None
    assert bool(destination.get_semantic(keys[1])) is commit_before_error
    assert destination.get_semantic(keys[2]) is None


@pytest.mark.asyncio
async def test_failed_copy_provenance_cannot_be_committed_by_a_later_write(env, monkeypatch):
    from kiro_crew import memory_schema

    source, destination = env.tiers[""], env.tiers["member-alice"]
    source.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")

    def failed_stamp(*args):
        raise ValueError("cannot stamp source")

    monkeypatch.setattr(memory_schema, "facet_stamp_params", failed_stamp)
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": "fact", "id": "project.database"}), owner=True)
    )
    assert json.loads(response.text)["results"][0]["outcome"] == "unconfirmed"
    destination.set_semantic("project.language", "Python", 1.0, "user_explicit")
    assert destination.get_semantic("project.database") is None
    assert destination.get_semantic("project.language") is not None


@pytest.mark.asyncio
async def test_internal_recall_uses_recorded_member_and_returns_bounded_evidence(env, member_proof):
    for name, marker in (
        ("", "GLOBALSECRET"),
        ("member-bob", "BOBSECRET"),
        ("member-alice", "ALICEFACT"),
    ):
        env.tiers[name].set_semantic(
            "project.database", f"PostgreSQL {marker}", 1.0, "user_explicit"
        )
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database"}, internal=True, proof=member_proof)
    )
    assert response.status == 200
    result = json.loads(response.text)
    assert result["store"] == "member-alice"
    assert result["algorithm_version"] == "v2"
    assert result["total_chars"] <= 3000
    assert "ALICEFACT" in response.text
    assert "GLOBALSECRET" not in response.text and "BOBSECRET" not in response.text
    assert result["retrieval"]["facts"][0]["retrieval"]["reason"] == "keyword_match"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,owner,internal",
    [
        ({"q": "database"}, False, False),
        ({"q": "database", "store": "member-bob"}, False, True),
        ({"q": "database", "store": "default"}, False, True),
    ],
)
async def test_caller_cannot_choose_another_members_recall(env, query, owner, internal):
    response = await memory_member.api_memory_recall(
        request(env, query=query, owner=owner, internal=internal)
    )
    assert response.status == 403


@pytest.mark.asyncio
async def test_owner_can_preview_a_selected_private_store(env):
    env.tiers["member-bob"].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database", "store": "member-bob"}, owner=True)
    )
    assert response.status == 200
    assert json.loads(response.text)["store"] == "member-bob"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_global_v1_recall_uses_the_same_explicit_tool_route(env, monkeypatch, owner):
    from kiro_crew import member_memory_auth

    env.tiers[""].set_semantic("project.database", "PostgreSQL V1FACT", 1.0, "user_explicit")
    env.tiers["member-alice"].set_semantic(
        "project.database", "PostgreSQL ALICEFACT", 1.0, "user_explicit"
    )
    env.state._slots["ui"] = SimpleNamespace(is_restricted=False, blocks_reads=False)
    env.metadata["dashboard:ui"] = {"memory_store": "default"}
    # A real host process without a protected member record is the legitimate
    # legacy caller. The authority check still runs when private stores exist.
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    response = await memory_member.api_memory_recall(
        request(
            env,
            query={"q": "PostgreSQL database"},
            owner=owner,
            internal=not owner,
            session="dashboard:ui",
        )
    )
    assert response.status == 200
    result = json.loads(response.text)
    assert result["algorithm_version"] == "v1"
    assert result["store"] == ""
    assert "V1FACT" in response.text and "ALICEFACT" not in response.text
    assert result["total_chars"] <= 3000


@pytest.mark.asyncio
async def test_temporary_session_cannot_recall(env):
    env.state._slots["alice"].blocks_reads = True
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True)
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "memory_reads_disabled"


@pytest.mark.asyncio
async def test_unknown_recorded_binding_fails_without_reading_global(env):
    env.metadata["dashboard:alice"]["memory_store"] = "missing-member"
    env.tiers[""].set_semantic("project.database", "GLOBALSECRET", 1.0, "user_explicit")
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True)
    )
    assert response.status == 503
    assert "GLOBALSECRET" not in response.text


@pytest.mark.asyncio
async def test_database_read_failure_returns_explicit_unavailability(
    env, monkeypatch, member_proof
):
    def unreadable(*args, **kwargs):
        raise OSError("disk unreadable")

    monkeypatch.setattr(env.tiers["member-alice"], "recall", unreadable)
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True, proof=member_proof)
    )
    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"


def test_mcp_recall_forwards_strict_identity_and_encoded_query(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "subagent:alice-run")
    get = mock.Mock(return_value={"store": "member-alice", "semantic_context": "known fact"})
    monkeypatch.setattr(mcp_core, "_get", get)
    result = json.loads(learn.memory_recall("memory_recall", {"query": "数据库 & PostgreSQL?"}))
    assert result["store"] == "member-alice"
    path = get.call_args.args[0]
    assert parse_qs(urlsplit(path).query) == {"q": ["数据库 & PostgreSQL?"]}
    assert get.call_args.kwargs == {"session_key": "subagent:alice-run"}


@pytest.mark.parametrize("query", [None, "", " ", "x" * 2001])
def test_invalid_mcp_recall_never_calls_gateway(monkeypatch, query):
    get = mock.Mock()
    monkeypatch.setattr(mcp_core, "_get", get)
    assert learn.memory_recall("memory_recall", {"query": query}).startswith("Error:")
    get.assert_not_called()


def test_unresolved_mcp_identity_never_falls_back_to_default_session(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    get = mock.Mock()
    monkeypatch.setattr(mcp_core, "_get", get)
    assert "established session" in learn.memory_recall("memory_recall", {"query": "database"})
    get.assert_not_called()


@pytest.mark.asyncio
async def test_owner_restore_reports_pending_without_touching_live_member(env):
    from kiro_crew import memory_backup

    tier = env.tiers["member-alice"]
    tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    path = env.home / "memory_stores" / "member-alice" / "memory.db"
    backup = await asyncio.to_thread(memory_backup.backup_store, path)
    tier.set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    response = await memory_admin.api_memory_restore(
        request(env, body={"store": "member-alice", "name": backup.name}, owner=True)
    )
    assert response.status == 200
    assert json.loads(response.text)["pending"] is True
    assert json.loads(response.text)["restart_required"] is True
    assert json.loads(tier.get_semantic("project.database")["value_json"]) == "SQLite"


@pytest.mark.asyncio
async def test_episode_owner_correction_preserves_identity_provenance_and_retry(env):
    tier = env.tiers["member-alice"]
    tier.write_episodic(
        "Postgres listens on port 5432 locally",
        defer_embedding=True,
        facets=memory_schema.MemoryFacets(derived_from="explicit-source", surface="owner_seed"),
    )
    row = tier.get_episodic_list()[0]
    body = {
        "store": "member-alice",
        "id": row["id"],
        "text": "Postgres listens on port 6432 locally",
        "tags": ["Database"],
        "importance": 0.9,
    }
    response = await memory_member.api_memory_episodic_correct(request(env, body=body, owner=True))
    assert response.status == 200 and json.loads(response.text)["changed"] is True
    updated = tier.get_episodic_list()[0]
    assert updated["id"] == row["id"] and updated["created_at"] == row["created_at"]
    assert updated["derived_from"] == "explicit-source" and updated["source"] == "user_explicit"
    assert updated["text"] == body["text"] and json.loads(updated["tags"]) == ["database"]
    assert any(event["event_type"] == "correct" for event in tier.get_events())
    retry = await memory_member.api_memory_episodic_correct(request(env, body=body, owner=True))
    assert retry.status == 200 and json.loads(retry.text)["changed"] is False
    foreign = await memory_member.api_memory_episodic_correct(
        request(env, body={**body, "store": "member-bob"}, owner=True)
    )
    assert foreign.status == 404 and env.tiers["member-bob"].get_episodic_list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"text": "short"},
        {"text": []},
        {"tags": [1]},
        {"tags": "tag"},
        {"importance": True},
        {"importance": float("nan")},
    ],
)
async def test_episode_correction_invalid_input_never_mutates(env, change):
    tier = env.tiers["member-alice"]
    tier.write_episodic("Postgres listens on port 5432 locally", defer_embedding=True)
    row = tier.get_episodic_list()[0]
    body = {"store": "member-alice", "id": row["id"], "text": "A valid corrected episode", **change}
    response = await memory_member.api_memory_episodic_correct(request(env, body=body, owner=True))
    assert response.status == 400
    assert tier.get_episodic_list()[0]["text"] == row["text"]


@pytest.mark.asyncio
async def test_episode_correction_owner_gate_v1_refusal_and_duplicate_conflict(env):
    tier = env.tiers["member-alice"]
    tier.write_episodic("Postgres listens on port 5432 locally", defer_embedding=True)
    tier.write_episodic("Postgres listens on port 6432 locally", defer_embedding=True)
    rows = tier.get_episodic_list()
    body = {"store": "member-alice", "id": rows[0]["id"], "text": rows[1]["text"]}
    response = await memory_member.api_memory_episodic_correct(
        request(env, body=body, internal=True)
    )
    assert response.status == 403
    response = await memory_member.api_memory_episodic_correct(request(env, body=body, owner=True))
    assert response.status == 409
    response = await memory_member.api_memory_episodic_correct(
        request(env, body={**body, "store": "default"}, owner=True)
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_pending_restore_status_survives_refresh_and_owner_can_cancel(env):
    from kiro_crew import memory_backup

    path = env.home / "memory_stores" / "member-alice" / "memory.db"
    backup = memory_backup.backup_store(path)
    response = await memory_admin.api_memory_restore(
        request(env, body={"store": "member-alice", "name": backup.name}, owner=True)
    )
    assert response.status == 200
    listing = await memory_admin.api_memory_backups(
        request(env, query={"store": "member-alice"}, owner=True)
    )
    status = json.loads(listing.text)
    assert status["pending"] and status["pending_restore"]["backup_name"] == backup.name
    denied = await memory_admin.api_memory_restore_cancel(
        request(env, body={"store": "member-alice"}, internal=True)
    )
    assert denied.status == 403
    result = await memory_admin.api_memory_restore_cancel(
        request(env, body={"store": "member-alice"}, owner=True)
    )
    assert result.status == 200 and json.loads(result.text)["cancelled"] is True
    listing = await memory_admin.api_memory_backups(
        request(env, query={"store": "member-alice"}, owner=True)
    )
    assert json.loads(listing.text)["pending"] is False
    assert json.loads(listing.text)["pending_restore"] is None
    assert backup.exists()


@pytest.mark.asyncio
async def test_episode_copy_retry_after_correction_and_forgetting_does_not_resurrect(env):
    source = env.tiers[""]
    source.write_episodic("Postgres listens on port 5432 locally", defer_embedding=True)
    source_id = source.get_episodic_list()[0]["id"]
    body = seed_body({"kind": "episode", "id": source_id})
    first = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert first.status == 200
    target = env.tiers["member-alice"]
    copied_id = target.get_episodic_list()[0]["id"]
    assert (
        target.correct_episodic(copied_id, "Postgres listens on port 6432 locally") == "corrected"
    )
    retry = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert json.loads(retry.text)["results"][0]["outcome"] == "existing"
    assert len(target.get_episodic_list()) == 1
    assert "6432" in target.get_episodic_list()[0]["text"]
    target.delete_episodic(copied_id)
    retry = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert json.loads(retry.text)["results"][0]["outcome"] == "existing"
    assert target.get_episodic_list() == []
