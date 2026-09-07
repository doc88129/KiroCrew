"""Private APIs need an actual process/session proof, never a shared local secret."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
from test_member_memory_api import env as _api_env
from test_member_memory_api import member_proof as _api_proof
from test_member_memory_api import request

from kiro_crew import member_memory_auth as auth
from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import _shared, memory, memory_member
from kiro_crew.mcp_gateway import socketsec

env = _api_env
member_proof = _api_proof


@pytest.mark.asyncio
async def test_real_middleware_accepts_mcp_recall_secret_but_still_checks_member_proof(
    env, member_proof
):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret="test-mcp-secret",
            )
        ]
    )
    app["state"] = env.state
    env.state._slots["bob"] = SimpleNamespace(is_restricted=False, blocks_reads=False)
    env.metadata["dashboard:bob"] = {"memory_store": "member-bob"}
    app.router.add_get("/api/memory/recall", memory_member.api_memory_recall)
    async with TestClient(TestServer(app)) as client:
        headers = {
            "X-Internal-Secret": "test-mcp-secret",
            "X-Session-Key": "dashboard:alice",
            auth.PROOF_HEADER: member_proof,
        }
        response = await client.get("/api/memory/recall?q=database", headers=headers)
        assert response.status == 200, await response.text()
        headers["X-Session-Key"] = "dashboard:bob"
        response = await client.get("/api/memory/recall?q=database", headers=headers)
        assert response.status == 403, await response.text()
        assert (await response.json())["code"] == "member_session_unverified"


@pytest.mark.parametrize(
    "platform,requested,effective,backend,kiro,delegates,expected",
    [
        ("win32", "standard", "standard", "namespace", True, False, False),
        ("linux", "off", "off", "namespace", True, False, False),
        ("linux", "standard", "standard", "none", True, False, False),
        ("linux", "standard", "standard", "namespace", True, False, True),
        ("linux", "off", "strict", "namespace", True, False, True),
        ("darwin", "standard", "standard", "sandbox-exec", True, True, False),
        ("darwin", "standard", "standard", "sandbox-exec", True, False, True),
        ("darwin", "standard", "standard", "sandbox-exec", False, True, True),
    ],
)
def test_private_execution_requires_the_enforced_outer_sandbox(
    monkeypatch, platform, requested, effective, backend, kiro, delegates, expected
):
    from kiro_crew import sandbox
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform=platform))
    config = SimpleNamespace(
        agent=SimpleNamespace(sandbox=requested, acp_backend="test", member_acp_backend="kas")
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    clamp = mock.Mock(return_value=effective)
    detect = mock.Mock(return_value=backend)
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", clamp)
    monkeypatch.setattr(sandbox, "detect_backend", detect)
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: delegates)
    monkeypatch.setattr(
        "kiro_crew.acp_backends.ACP_BACKENDS_INTERNAL_SANDBOX", {"test"} if kiro else set()
    )
    assert auth.private_memory_execution_supported() is expected
    if platform == "win32":
        clamp.assert_not_called()
        detect.assert_not_called()
    else:
        clamp.assert_called_once_with(requested)
        if expected:
            detect.assert_called_once_with(config_mode=effective)


@pytest.mark.parametrize("default,member,expected", [("claude", "", False), ("", "kas", True)])
def test_macos_member_guard_uses_the_members_actual_backend(monkeypatch, default, member, expected):
    from kiro_crew import sandbox
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="darwin"))
    config = SimpleNamespace(
        agent=SimpleNamespace(sandbox="standard", acp_backend=default, member_acp_backend=member)
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", lambda mode: mode)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kw: "sandbox-exec")
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: True)
    assert auth.private_memory_execution_supported(session_key="dashboard:member-alice") is expected


@pytest.mark.parametrize("platform,mechanism", [("linux", "namespace"), ("darwin", "sandbox-exec")])
@pytest.mark.parametrize("backend", ["", "claude", "kas", "codex"])
@pytest.mark.parametrize("session_key", ["dashboard:member-alice", "subagent:private-task"])
def test_private_execution_requires_direct_mcp_tools(
    monkeypatch, platform, mechanism, backend, session_key
):
    from kiro_crew import sandbox
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import UnknownMemoryStore

    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform=platform))
    config = SimpleNamespace(
        agent=SimpleNamespace(
            sandbox="standard",
            acp_backend=backend,
            member_acp_backend=backend,
        )
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", lambda mode: mode)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kw: mechanism)
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: False)
    if backend == "codex":
        with pytest.raises(UnknownMemoryStore, match="Codex ACP.*Use Kiro, Claude Code or KAS"):
            auth.require_private_memory_execution(session_key=session_key)
    else:
        auth.require_private_memory_execution(session_key=session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("weak_marker", [False, True])
async def test_shared_secret_and_session_header_cannot_read_private_memory(env, weak_marker):
    req = request(env, query={"q": "database"}, internal=True)
    req["peer_verified"] = weak_marker
    response = await memory_member.api_memory_recall(req)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_valid_proof_cannot_be_replayed_as_another_member(env, member_proof):
    env.metadata["dashboard:bob"] = {"memory_store": "member-bob"}
    req = request(env, session="dashboard:bob", internal=True, proof=member_proof)
    name, refusal = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    assert name == "" and refusal.status == 403
    assert json.loads(refusal.text)["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_same_member_proof_authorizes_the_lesson_write_target(env, member_proof):
    req = request(env, internal=True, proof=member_proof)
    name, refusal = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    assert name == "member-alice" and refusal is None


@pytest.mark.asyncio
async def test_global_v1_internal_lessons_keep_existing_authorization(env, monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: os.getpid())
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    req = request(env, session="dashboard:legacy", internal=True)
    name, refusal = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    assert name == "" and refusal is None


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ["", "dashboard:ui", "dashboard:legacy"])
@pytest.mark.parametrize("forward_proof", [False, True])
@pytest.mark.parametrize("route", ["lessons", "global_list", "consolidate"])
async def test_member_cannot_downgrade_to_global_or_omit_its_identity(
    env, member_proof, monkeypatch, declared, forward_proof, route
):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: os.getpid())
    req = request(
        env,
        session=declared,
        internal=True,
        proof=member_proof if forward_proof else "",
        body={"key": "dashboard:legacy"} if route == "consolidate" else None,
    )
    if route == "lessons":
        _, response = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    elif route == "global_list":
        _, response = await _shared.resolve_requested_memory_store(req, env.state, "memory.list")
    else:
        env.state.consolidator = mock.Mock()
        response = await memory.api_memory_consolidate(req)
        env.state.consolidator._consolidate.assert_not_called()
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_correct_member_header_cannot_select_implicit_global_list(env, member_proof):
    req = request(env, internal=True, proof=member_proof)
    _, response = await _shared.resolve_requested_memory_store(req, env.state, "memory.list")
    assert response.status == 403


@pytest.mark.asyncio
async def test_unverifiable_peer_cannot_use_global_when_private_members_exist(env, monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: None)
    req = request(env, session="", internal=True)
    _, response = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.list")
    assert response.status == 403


@pytest.mark.asyncio
async def test_pure_v1_installation_keeps_legacy_internal_access(env, monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: None)
    monkeypatch.setattr(auth, "private_memory_boundaries_active", lambda: False)
    req = request(env, session="", internal=True)
    name, response = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.list")
    assert name == "" and response is None


@pytest.mark.asyncio
async def test_member_cannot_consolidate_a_different_members_session(env, member_proof):
    env.metadata["dashboard:bob"] = {"memory_store": "member-bob"}
    env.state.consolidator = mock.Mock()
    req = request(env, body={"key": "dashboard:bob"}, internal=True, proof=member_proof)
    response = await memory.api_memory_consolidate(req)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_session_unverified"
    env.state.consolidator._consolidate.assert_not_called()


def test_proof_is_invalid_after_process_rekey(env, member_proof):
    assert auth.verify_member_session_proof(member_proof, "dashboard:alice")
    auth.publish_member_session_pid(os.getpid(), "dashboard:bob", memory_store="member-bob")
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


def test_global_rekey_does_not_publish_a_private_identity(env, member_proof, monkeypatch):
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    auth.publish_member_session_pid(os.getpid(), "dashboard:first-global", memory_store="")
    assert auth.protected_member_session_for_pid(os.getpid()) is None
    auth.publish_member_session_pid(os.getpid(), "dashboard:second-global", memory_store="")
    assert auth.protected_member_session_for_pid(os.getpid()) is None
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["private", "corrupt", "unverifiable", "host"])
async def test_member_cannot_promote_local_secret_to_owner_token(
    env, member_proof, monkeypatch, identity
):
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import core

    req = request(env)
    req.app["local_secret"] = "test-local-secret"
    req = make_mocked_request(
        "GET", "/api/token/local", app=req.app, headers={"X-Local-Secret": "test-local-secret"}
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda value: True)
    monkeypatch.setattr(
        auth,
        "_request_peer_pid",
        lambda request: None if identity == "unverifiable" else os.getpid(),
    )
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    if identity == "host":
        auth._binding_path(os.getpid(), env.home).unlink()
    elif identity == "corrupt":
        auth._binding_path(os.getpid(), env.home).write_text("{broken", encoding="utf-8")
    issue = mock.Mock(return_value="owner-token")
    monkeypatch.setattr(core, "generate_token", issue)
    response = await core.api_token_local(req)
    assert response.status == (200 if identity == "host" else 403)
    if identity != "host":
        issue.assert_not_called()
        assert json.loads(response.text)["code"] == "member_owner_token_refused"


@pytest.mark.parametrize("strict", [False, True])
def test_private_mcp_uses_protected_ancestry_without_legacy_pid_files(
    env, member_proof, monkeypatch, strict
):
    from kiro_crew import mcp_core

    monkeypatch.setattr(mcp_core, "current_caller", lambda: None)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:forged-global")
    resolve = mcp_core._resolve_session_key_strict if strict else mcp_core._resolve_session_key
    assert resolve() == "dashboard:alice"


def test_proof_is_invalid_after_process_incarnation_changes(env, member_proof, monkeypatch):
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "recycled")
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


def test_expired_or_tampered_proof_does_not_authorize(env, member_proof, monkeypatch):
    assert not auth.verify_member_session_proof(member_proof + "0", "dashboard:alice")
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 61)
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


def test_writable_legacy_pid_mapping_does_not_grant_a_member(env, monkeypatch):
    pid = os.getpid()
    (env.home / f"session_pid_{pid}.txt").write_text("dashboard:bob", encoding="utf-8")
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    assert auth.verified_member_session_for_pid(pid) == ""
    assert auth.issue_member_session_proof("dashboard:bob", pid) == ""


def test_corrupt_nearest_identity_cannot_borrow_an_ancestor(env, member_proof, monkeypatch):
    pid = os.getpid()
    path = auth._binding_path(pid, env.home)
    path.write_text("{corrupt", encoding="utf-8")
    parent_probe = mock.Mock(return_value=pid + 1)
    monkeypatch.setattr(platform_compat, "get_ppid", parent_probe)
    assert auth.verified_member_session_for_pid(pid) == ""
    parent_probe.assert_not_called()


@pytest.mark.parametrize("declared", ["dashboard:alice", "dashboard:bob"])
def test_unix_peer_uses_protected_real_process_identity(env, member_proof, monkeypatch, declared):
    req = request(env, session=declared, internal=True)
    sock = object()
    monkeypatch.setattr("kiro_crew.dashboard.token_auth._unix_request_socket", lambda req: sock)
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda sock: socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda sock: os.getpid())
    assert auth.private_memory_request_verified(req) is (declared == "dashboard:alice")


@pytest.mark.parametrize("peer_pid", [None, 0])
def test_unverifiable_unix_peer_does_not_use_header(env, member_proof, monkeypatch, peer_pid):
    req = request(env, internal=True)
    monkeypatch.setattr("kiro_crew.dashboard.token_auth._unix_request_socket", lambda req: object())
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda sock: socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda sock: peer_pid)
    assert not auth.private_memory_request_verified(req)


def test_tcp_proof_uses_exact_kernel_endpoints_and_protected_identity(
    env, member_proof, monkeypatch
):
    endpoints = {"sockname": ("127.0.0.1", 9001), "peername": ("127.0.0.1", 42010)}
    req = SimpleNamespace(
        headers={"X-Session-Key": "dashboard:alice"},
        get=lambda name: name == "internal_auth",
        transport=SimpleNamespace(get_extra_info=endpoints.get),
    )
    monkeypatch.setattr("kiro_crew.dashboard.token_auth._unix_request_socket", lambda req: None)
    resolve = mock.Mock(return_value=os.getpid())
    monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", resolve)
    assert auth.private_memory_request_verified(req)
    resolve.assert_called_once_with(endpoints["sockname"], endpoints["peername"])
    endpoints["peername"] = ("203.0.113.1", 42010)
    resolve.reset_mock()
    assert not auth.private_memory_request_verified(req)
    resolve.assert_not_called()
