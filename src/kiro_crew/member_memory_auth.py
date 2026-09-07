"""Gateway-owned process bindings and delegated proofs for private memory APIs.

The shared internal secret authenticates a local component, never a member.
Member authority follows kernel process ancestry into a sandbox-readonly record.
Absent ancestry is not host authority: inherited OS isolation must also agree.
Pooled MCP backends receive a short-lived proof from their trusted gateway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

PROOF_HEADER = "X-Member-Session-Proof"
PROOF_META_KEY = "memberMemoryProof"
_PROOF_TTL = 60
_MAX_RECORD_BYTES = 4096


def _binding_path(pid: int, home: Path) -> Path:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("Invalid member process identity")
    path = home.resolve() / "member-memory-bindings" / "pids" / f"{pid}.json"
    if path.resolve() != path:
        raise ValueError("Member process identity path is redirected")
    return path


def _session_binding_path(session_key: str) -> Path:
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("A private session key is required")
    digest = hashlib.sha256(session_key.encode()).hexdigest()
    path = config_dir().resolve() / "member-memory-bindings" / "sessions" / digest / "memory.json"
    if path.resolve() != path:
        raise ValueError("The private session binding path is redirected")
    return path


def read_private_session_store(session_key: str) -> str | None:
    """Read the immutable gateway binding; missing/corrupt existing records refuse."""
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    path = _session_binding_path(session_key)
    if not path.parent.exists():
        return None
    raw = _read_regular_nofollow(path)
    if raw is None:
        raise ValueError("The protected member session binding is missing or unreadable")
    row = json.loads(raw)
    if (
        not isinstance(row, dict)
        or row.get("version") != 1
        or row.get("session_key") != session_key
    ):
        raise ValueError("The protected member session binding is invalid")
    store = row.get("memory_store")
    if not isinstance(store, str) or not store or store == "default":
        raise ValueError("The protected member session has no private store")
    return store


def bind_private_session_store(session_key: str, memory_store: str) -> None:
    """First trusted private preparation pins this session permanently to its member."""
    from kiro_crew.memory_stores import memory_store_version, require_memory_store

    require_memory_store(memory_store)
    if memory_store_version(memory_store) != 2:
        raise ValueError("Only private V2 memory can bind a private session")
    path = _session_binding_path(session_key)
    for directory in (path.parent.parent.parent, path.parent.parent, path.parent):
        platform_compat.make_owner_only_dir(directory)
        platform_compat.restrict_dir_to_owner(directory)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if read_private_session_store(session_key) != memory_store:
            raise ValueError(
                "This session is already bound to another member; open a new conversation"
            )
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": 1, "session_key": session_key, "memory_store": memory_store}, handle
            )


def publish_member_session_pid(
    pid: int, session_key: str, *, home: Path | None = None, memory_store: str | None = None
) -> None:
    """Trusted publisher only; an unknown process incarnation grants nothing."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return
    path = _binding_path(pid, home or config_dir())
    store = private_memory_store_for_session(session_key) if memory_store is None else memory_store
    start = platform_compat.get_process_start_id(pid)
    if not start or not isinstance(session_key, str) or not session_key:
        path.unlink(missing_ok=True)
        return
    for directory in (path.parent.parent, path.parent):
        platform_compat.make_owner_only_dir(directory)
        platform_compat.restrict_dir_to_owner(directory)
    atomic_write(
        path,
        json.dumps(
            {
                "version": 2,
                "session_key": session_key,
                "process_start": start,
                "memory_store": store,
            }
        ),
    )


def _private_memory_mcp_failure(backend: str) -> str:
    from kiro_crew.acp_backends import ACP_BACKEND_CODEX

    if backend == ACP_BACKEND_CODEX:
        return (
            "Codex ACP cannot run private member MCP tools directly. Use Kiro, Claude Code or KAS: "
            "set the member backend for private chat, and the default backend for Crew tasks, "
            "schedules and memory consolidation."
        )
    return ""


def require_private_memory_mcp_backend(backend: str) -> None:
    """Validate the actual private runtime backend before constructing it."""
    reason = _private_memory_mcp_failure(backend)
    if reason:
        from kiro_crew.memory_stores import UnknownMemoryStore

        raise UnknownMemoryStore(reason + " Global Memory V1 was not used.")


def private_memory_execution_supported(*, session_key: str = "") -> bool:
    """Use the spawn layer's mode/floor/backend decisions, excluding delegation."""
    return not _private_memory_execution_failure(session_key=session_key)


def _private_memory_execution_failure(*, session_key: str = "") -> str:
    if sys.platform == "win32":
        return "Native Windows cannot enforce private member filesystem isolation. Use the WSL/Linux gateway."
    from kiro_crew import sandbox
    from kiro_crew.acp_backends import ACP_BACKENDS_INTERNAL_SANDBOX
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.members import select_provider_backend

    try:
        config = KiroCrewConfig.load()
        backend = select_provider_backend(
            session_key, config.agent.member_acp_backend, config.agent.acp_backend
        )
        mcp_failure = _private_memory_mcp_failure(backend)
        if mcp_failure:
            return mcp_failure
        mode = sandbox._clamp_sandbox_mode(config.agent.sandbox)
        if mode == "off":
            return "Crew's OS sandbox is off. Enable agent.sandbox and restart the gateway before running private members."
        if sys.platform == "darwin":
            if backend in ACP_BACKENDS_INTERNAL_SANDBOX and sandbox.kiro_internal_sandbox_enabled():
                return "Kiro internal sandbox delegation bypasses Crew's private member filesystem fences. Disable that delegation and restart with Crew's outer Seatbelt sandbox."
        if sandbox.detect_backend(config_mode=mode) not in {"namespace", "sandbox-exec"}:
            mechanism = (
                "outer Seatbelt sandbox"
                if sys.platform == "darwin"
                else "Linux user/mount namespaces"
            )
            return f"Crew could not activate {mechanism}. Restore OS sandbox support before running private members."
        return ""
    except Exception as exc:
        return f"Private member sandbox configuration could not be verified ({type(exc).__name__}). Check the gateway's sandbox configuration and restart."


def require_private_memory_execution(*, session_key: str = "") -> None:
    """Fail before provider startup when private files cannot be OS-isolated."""
    if not private_memory_execution_supported(session_key=session_key):
        from kiro_crew.memory_stores import UnknownMemoryStore

        reason = _private_memory_execution_failure(session_key=session_key)
        raise UnknownMemoryStore(
            (reason or "Private member OS filesystem isolation could not be verified.")
            + " Member management remains available; Global Memory V1 was not used."
        )


def verified_member_session_for_pid(peer_pid: int, *, home: Path | None = None) -> str:
    """Resolve the first recorded real ancestor; corrupt or recycled records refuse."""
    return protected_member_session_for_pid(peer_pid, home=home) or ""


def protected_member_session_for_pid(peer_pid: int, *, home: Path | None = None) -> str | None:
    """None means no private binding, not host proof; empty means invalid identity."""
    binding = _protected_member_binding_for_pid(peer_pid, home=home)
    return None if binding is None or (binding[0] and not binding[1]) else binding[0]


def _protected_member_binding_for_pid(
    peer_pid: int, *, home: Path | None = None
) -> tuple[str, str] | None:
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    base = home or config_dir()
    pid = peer_pid
    seen: set[int] = set()
    for _ in range(128):
        if not isinstance(pid, int) or pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        try:
            path = _binding_path(pid, base)
            if path.exists():
                raw = _read_regular_nofollow(path)
                if raw is None:
                    return "", ""
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get("version") not in (1, 2):
                    return "", ""
                start = platform_compat.get_process_start_id(pid)
                key = row.get("session_key")
                store = row.get("memory_store")
                if (
                    not start
                    or start != row.get("process_start")
                    or not isinstance(key, str)
                    or not key
                    or not isinstance(store, str)
                    or store == "default"
                    or (not store and row.get("version") != 2)
                ):
                    return "", ""
                if not store and sys.platform == "linux":
                    # A V1 runtime is positively published too, but a nested
                    # private namespace cannot inherit that runtime's authority.
                    if platform_compat.process_namespaces_match(peer_pid, pid) is not True:
                        return "", ""
                return key, store
            pid = platform_compat.get_ppid(pid)
        except (OSError, ValueError, RuntimeError):
            return "", ""
    return "", ""


def _verified_host_process(pid: int) -> bool:
    """Positive host provenance survives loss of every recorded ancestor.

    Linux private descendants retain a different user/mount namespace, even
    after reparenting or a gateway restart. Seatbelt is inherited on macOS. An
    unknown sandboxed caller therefore fails closed instead of becoming V1 or
    owner. Native Windows has no private runtime: its spawn gate refuses one.
    """
    if sys.platform == "linux":
        return platform_compat.process_namespaces_match(pid, os.getpid()) is True
    if sys.platform == "darwin":
        return platform_compat.process_is_sandboxed(pid) is False
    return sys.platform == "win32"


def _verified_global_process(pid: int) -> bool:
    binding = _protected_member_binding_for_pid(pid)
    if binding is not None:
        return bool(binding[0] and not binding[1])
    return _verified_host_process(pid)


def _proof_key(*, create: bool) -> bytes | None:
    path = config_dir().resolve() / "memory_stores" / ".member-api-key"
    if path.resolve() != path:
        return None
    if create:
        platform_compat.make_owner_only_dir(path.parent)
        platform_compat.restrict_dir_to_owner(path.parent)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secrets.token_bytes(32))
    try:
        with path.open("rb") as handle:
            key = handle.read(33)
        return key if len(key) == 32 else None
    except OSError:
        return None


def issue_member_session_proof(session_key: str, peer_pid: int) -> str:
    """Trusted MCP gateway only; claimed headers/metadata cannot mint a proof."""
    if verified_member_session_for_pid(peer_pid) != session_key:
        return ""
    start = platform_compat.get_process_start_id(peer_pid)
    key = _proof_key(create=True)
    if not session_key or not start or key is None:
        return ""
    body = json.dumps(
        {"v": 1, "s": session_key, "p": peer_pid, "i": start, "e": int(time.time()) + _PROOF_TTL},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
    digest = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def verify_member_session_proof(proof: str, session_key: str) -> bool:
    """A proof remains valid only while its live process retains this session."""
    if not isinstance(proof, str) or len(proof) > _MAX_RECORD_BYTES:
        return False
    try:
        payload, supplied = proof.split(".")
        key = _proof_key(create=False)
        if key is None or not hmac.compare_digest(
            supplied, hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        ):
            return False
        row = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        now = time.time()
        if (
            not isinstance(row, dict)
            or row.get("v") != 1
            or row.get("s") != session_key
            or not isinstance(row.get("e"), int)
            or not now < row["e"] <= now + _PROOF_TTL
        ):
            return False
        pid = row.get("p")
        return bool(
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and row.get("i")
            and platform_compat.get_process_start_id(pid) == row["i"]
            and verified_member_session_for_pid(pid) == session_key
        )
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def private_memory_request_verified(request: Any) -> bool:
    """Unverifiable callers always fail closed, including transport/read failures."""
    try:
        return _private_memory_request_verified(request)
    except Exception:
        return False


def _private_memory_request_verified(request: Any) -> bool:
    """Positive member proof, independent of the caller-supplied session header."""
    key = request.headers.get("X-Session-Key", "")
    actual, verified = memory_request_identity(request)
    return bool(key and verified and actual == key)


def memory_request_identity(request: Any) -> tuple[str | None, bool]:
    """Authenticate the caller before interpreting any requested store or header.

    A verified None is an unowned process. An unverifiable identity grants no
    downgrade authority, even when the requested destination happens to be V1.
    """
    try:
        if request.get("internal_auth") is not True:
            return None, False
        proof = request.headers.get(PROOF_HEADER, "")
        if proof:
            if not isinstance(proof, str) or len(proof) > _MAX_RECORD_BYTES:
                return None, False
            payload = proof.split(".")[0]
            row = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            key = row.get("s") if isinstance(row, dict) else None
            if not isinstance(key, str) or not key or not verify_member_session_proof(proof, key):
                return None, False
            return key, True
        pid = _request_peer_pid(request)
        if not isinstance(pid, int) or not platform_compat.get_process_start_id(pid):
            return None, False
        actual = protected_member_session_for_pid(pid)
        if actual is None and not _verified_global_process(pid):
            return None, False
        return actual, actual != ""
    except Exception:
        return None, False


def memory_request_bound_store(request: Any) -> str | None:
    """Return the process-protected target, never writable transcript metadata."""
    proof = request.headers.get(PROOF_HEADER, "")
    if proof:
        payload = proof.split(".")[0]
        row = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        pid = row.get("p")
    else:
        pid = _request_peer_pid(request)
    if not isinstance(pid, int):
        return None
    binding = _protected_member_binding_for_pid(pid)
    return binding[1] if binding and binding[0] else None


def private_memory_store_for_session(session_key: str | None) -> str:
    """Trusted gateway-only resolution used before process allocation/publication."""
    if not session_key or not private_memory_boundaries_active():
        return ""
    from kiro_crew.context import store_of_session
    from kiro_crew.history import ConversationLog
    from kiro_crew.memory_stores import memory_store_version

    store = store_of_session(ConversationLog(), session_key)
    return store if store and memory_store_version(store) == 2 else ""


def private_memory_boundaries_active() -> bool:
    """Pure V1 installations retain their existing internal API contract."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        if (config_dir() / "member-memory-bindings" / "sessions").exists():
            return True
        return any(
            store.memory_version == 2 for store in KiroCrewConfig.load().memory_stores.values()
        )
    except Exception:
        return True


def local_owner_bootstrap_allowed(request: Any) -> bool:
    """The shared local secret cannot promote a private member into the owner."""
    try:
        if not private_memory_boundaries_active():
            return True
        pid = _request_peer_pid(request)
        return bool(
            isinstance(pid, int)
            and platform_compat.get_process_start_id(pid)
            and protected_member_session_for_pid(pid) is None
            and _verified_host_process(pid)
        )
    except Exception:
        return False


def _request_peer_pid(request: Any) -> int | None:
    from kiro_crew.dashboard.token_auth import _unix_request_socket
    from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid

    sock = _unix_request_socket(request)
    if sock is not None:
        if check_peer_is_self(sock) is not PeerCredResult.MATCH:
            return None
        pid = get_peer_pid(sock)
    else:
        from kiro_crew.dashboard.origin import is_loopback

        transport = getattr(request, "transport", None)
        if transport is None:
            return None
        server = transport.get_extra_info("sockname")
        client = transport.get_extra_info("peername")
        if not (
            isinstance(server, tuple)
            and isinstance(client, tuple)
            and len(server) >= 2
            and len(client) >= 2
            and is_loopback(server[0])
            and is_loopback(client[0])
        ):
            return None
        resolve_tcp = getattr(platform_compat, "get_tcp_peer_pid", None)
        if resolve_tcp is None:
            return None
        pid = resolve_tcp(server[:2], client[:2])
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None
