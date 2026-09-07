"""Member-only Global V1 filesystem separation, including real late-file probes."""

from __future__ import annotations

import ast
import json
import selectors
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew import sandbox


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "crew"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    monkeypatch.delenv("KIROCREW_MCP_SOCKET", raising=False)
    monkeypatch.delenv("MC_MCP_SOCKET", raising=False)
    monkeypatch.setattr(sandbox, "config_dir", lambda: root)
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(root)])
    return root


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
def test_default_launcher_remains_identical_and_private_view_is_explicit(home):
    default = sandbox._build_launcher_script("standard")
    assert default == sandbox._build_launcher_script("standard", private_memory=False)
    assert "_private_cwd" not in default
    private = sandbox._build_launcher_script("standard", private_memory=True)
    ast.parse(private)
    assert "_private_cwd" in private and "os.chdir(_private_cwd)" in private


def test_private_seatbelt_rules_override_visible_carveout_and_preserve_v1(home):
    default = sandbox._build_seatbelt_profile("standard")
    assert default == sandbox._build_seatbelt_profile("standard", private_memory=False)
    private = sandbox._build_seatbelt_profile(
        "standard", private_memory=True, extra_visible_dirs=(str(home),)
    )
    for rule in sandbox._private_memory_seatbelt_rules():
        assert rule in private
        if "(regex " in rule:
            assert rule not in default
    assert "lessons" in private and "backups" in private


@pytest.mark.parametrize(
    "leaf", ["mcp-gateway", "kirocrew-mcp-gateway.sock", "mc-mcp-gateway.sock"]
)
def test_private_seatbelt_blocks_shared_broker_file_and_socket_access(home, leaf):
    default = sandbox._build_seatbelt_profile("standard")
    private = sandbox._build_seatbelt_profile(
        "standard", private_memory=True, extra_visible_dirs=(str(home),)
    )
    predicate = "subpath" if leaf == "mcp-gateway" else "literal"
    for operation in ("file-read*", "file-write*", "file-link"):
        rule = f"(deny {operation} ({predicate} {json.dumps(str(home) + '/' + leaf)}))"
        assert rule in private
        assert rule not in default
    socket_rule = f"(deny network-outbound (remote unix-socket ({predicate} {json.dumps(str(home) + '/' + leaf)})))"
    assert socket_rule in private
    assert socket_rule not in default
    assert json.dumps(str(home) + "/workspace/" + leaf) not in private
    assert "(deny network-outbound (subpath " + json.dumps(str(home) + "/run") not in private


@pytest.mark.parametrize(
    "endpoint",
    [
        "mcp-gateway/gateway.sock",
        "mcp-gateway/nested/custom.sock",
        "kirocrew-mcp-gateway.sock",
        "mc-mcp-gateway.sock",
    ],
)
def test_private_broker_endpoint_accepts_reserved_paths_in_each_data_home(
    home, monkeypatch, endpoint
):
    alternate = home.parent / "alternate"
    alternate.mkdir()
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(home), str(alternate)])
    for root in (home, alternate):
        sandbox._validate_private_mcp_gateway_socket(str(root / endpoint))


@pytest.mark.parametrize(
    "source", ["constructor", "persisted", "KIROCREW_MCP_SOCKET", "MC_MCP_SOCKET", "child"]
)
def test_private_broker_endpoint_refuses_uncontained_custom_socket(home, monkeypatch, source):
    project = home.parent / "project"
    project.mkdir()
    code = project / "code.py"
    code.write_text("project code")
    endpoint = str(project / "custom.sock")
    overrides = ()
    argument = ""
    if source == "constructor":
        argument = endpoint
    elif source == "persisted":
        (home / "config.json").write_text(
            json.dumps({"mcp_gateway": {"stub_servers": [], "socket_path": endpoint}})
        )
    elif source == "child":
        overrides = (str(home / "mcp-gateway" / "safe.sock"), endpoint)
    else:
        monkeypatch.setenv(source, endpoint)
    with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
        sandbox._validate_private_mcp_gateway_socket(argument, overrides)
    assert code.read_text() == "project code"
    assert not (project / "custom.sock").exists()


@pytest.mark.parametrize("stub_servers", [[], ["builder"]])
def test_private_broker_validates_persisted_socket_independently_of_routing(home, stub_servers):
    (home / "config.json").write_text(
        json.dumps(
            {
                "mcp_gateway": {
                    "stub_servers": stub_servers,
                    "socket_path": str(home / "mcp-gateway" / "custom.sock"),
                }
            }
        )
    )
    sandbox._validate_private_mcp_gateway_socket(
        socket_overrides=(str(home / "mc-mcp-gateway.sock"),)
    )


def test_private_broker_validates_other_known_data_homes(home, monkeypatch):
    alternate = home.parent / "alternate"
    alternate.mkdir()
    (alternate / "config.json").write_text(
        json.dumps(
            {"mcp_gateway": {"stub_servers": [], "socket_path": str(home.parent / "old.sock")}}
        )
    )
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(home), str(alternate)])
    with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
        sandbox._validate_private_mcp_gateway_socket()


def test_private_broker_refuses_relative_child_paths_even_when_gateway_cwd_is_safe(
    home, monkeypatch
):
    monkeypatch.chdir(home)
    with pytest.raises(RuntimeError, match="using an absolute path"):
        sandbox._validate_private_mcp_gateway_socket(socket_overrides=("mcp-gateway/gateway.sock",))


@pytest.mark.parametrize(
    "contents", ["{broken", "[]", '{"mcp_gateway": null}', '{"mcp_gateway": {"socket_path": null}}']
)
def test_private_broker_refuses_unverifiable_persisted_config(home, contents):
    (home / "config.json").write_text(contents)
    with pytest.raises(RuntimeError, match="cannot verify the configured MCP socket"):
        sandbox._validate_private_mcp_gateway_socket()


def test_private_broker_refuses_unreadable_persisted_config(home, monkeypatch):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == home / "config.json":
            raise PermissionError("fixture cannot read config")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(RuntimeError, match="cannot verify the configured MCP socket"):
        sandbox._validate_private_mcp_gateway_socket()


def test_v1_profile_does_not_apply_private_broker_config_validation(home):
    before = sandbox._build_seatbelt_profile("standard")
    (home / "config.json").write_text("{broken")
    assert sandbox._build_seatbelt_profile("standard") == before


def test_private_broker_endpoint_refuses_redirected_reserved_directory(home):
    outside = home.parent / "outside"
    outside.mkdir()
    make_dir_link(home / "mcp-gateway", outside)
    with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
        sandbox._validate_private_mcp_gateway_socket()


def test_private_broker_endpoint_resolves_alias_into_hidden_namespace(home):
    broker = home / "mcp-gateway"
    broker.mkdir()
    make_dir_link(home / "broker-alias", broker)
    sandbox._validate_private_mcp_gateway_socket(str(home / "broker-alias" / "gateway.sock"))


def test_private_broker_endpoint_fails_closed_when_origin_cannot_be_resolved(home, monkeypatch):
    unresolved = home / "mcp-gateway" / "unreadable.sock"
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == unresolved:
            raise PermissionError("fixture cannot resolve endpoint")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(RuntimeError, match="cannot resolve the shared MCP socket"):
        sandbox._validate_private_mcp_gateway_socket(str(unresolved))


@pytest.mark.parametrize(
    "mode,backend,nested",
    [("off", "namespace", False), ("auto", "none", False), ("auto", "namespace", True)],
)
def test_actual_private_spawn_cannot_use_unconfined_or_nested_bypass(
    monkeypatch, mode, backend, nested
):
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: nested)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: backend)
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="Private member memory"):
        sandbox.wrap_argv(
            [sys.executable], mode=mode, private_memory=True, first_party_fixed_argv=True
        )


@pytest.mark.asyncio
async def test_async_wrapper_passes_private_flag_only_when_true():
    calls = []

    def prepare(argv, **kwargs):
        calls.append(kwargs)
        return argv, None

    await sandbox.wrap_argv_async([sys.executable], _prepare=prepare)
    await sandbox.wrap_argv_async([sys.executable], private_memory=True, _prepare=prepare)
    await sandbox.wrap_argv_async(
        [sys.executable],
        private_memory=True,
        private_mcp_gateway_socket="reserved.sock",
        private_mcp_gateway_socket_overrides=("other-reserved.sock",),
        _prepare=prepare,
    )
    await sandbox.wrap_argv_async(
        [sys.executable],
        private_mcp_gateway_socket="ignored-for-v1.sock",
        private_mcp_gateway_socket_overrides=("also-ignored-for-v1.sock",),
        _prepare=prepare,
    )
    assert calls == [
        {"mode": "auto"},
        {"mode": "auto", "private_memory": True},
        {
            "mode": "auto",
            "private_memory": True,
            "private_mcp_gateway_socket": "reserved.sock",
            "private_mcp_gateway_socket_overrides": ("other-reserved.sock",),
        },
        {"mode": "auto"},
    ]


_CHILD = """import json,logging,os,sys
from pathlib import Path
h=Path(os.environ['KIROCREW_HOME']); out={}
from kiro_crew.config.paths import private_runtime_log_dir
out['diagnostic_route_valid']=(private_runtime_log_dir()==h/'agent-logs') if sys.argv[1]=='private' else private_runtime_log_dir() is None
if sys.argv[1] == 'private':
 from kiro_crew.cli import _setup_cli_logging
 from kiro_crew.config.paths import ensure_data_home
 from kiro_crew.sel import sel
 ensure_data_home()
 _setup_cli_logging('status',1)
 logging.getLogger('kiro_crew').warning('private namespace log canary')
 out['private_log']='private namespace log canary' in (h/'agent-logs'/f'member-{os.getpid()}.log').read_text()
 sel().log_api_access(caller='filesystem-probe',operation='private.canary',outcome='allowed',critical=True)
 out['private_audit']='private.canary' in (h/'agent-logs'/f'audit-{os.getpid()}'/'security_events.jsonl').read_text()
print('READY',flush=True);sys.stdin.readline()
for name in ('memory.db','memory.db-wal','memory.db.superseded.future','tmpfuture.tmp',
             'lessons.jsonl','workspace/memory/preferences.md','backups/memory.old.db',
             'home-alias/memory.db','temporary-alias','logs-alias/member-other/other.log',
             'agent-logs/member-other/other.log','mcp-gateway/late.txt','broker-alias/late.txt',
             'kirocrew-mcp-gateway.sock','mc-mcp-gateway.sock'):
 try: (h/name).read_bytes();out[name]=True
 except OSError:out[name]=False
try: (h/'memory.db').write_text('member wrote global');out['global_write']=True
except OSError:out['global_write']=False
out['late_binding']=(h/'member-memory-bindings'/'late.txt').read_text()=='binding'
out['late_listener']=(h/'run'/'gateway.secret').read_text()=='secret'
try: (h/'memory_stores'/'.execution-logs'/'member-other'/'other.log').read_bytes();out['hidden_logs_denied']=False
except OSError:out['hidden_logs_denied']=True
out['code_read']=(h/'workspace'/'project'/'code.py').read_text()=='original'
out['broker_named_project']=(h/'workspace'/'mcp-gateway'/'code.py').read_text()=='project code'
(h/'workspace'/'mcp-gateway'/'new.py').write_text('member code')
(h/'workspace'/'project'/'new.py').write_text('member code')
try: Path('../memory.db').read_bytes();out['relative_global_read']=True
except OSError:out['relative_global_read']=False
print(json.dumps(out),flush=True)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux user/mount namespaces")
@pytest.mark.parametrize("private", [False, True])
def test_kernel_hides_replaced_and_future_global_memory_preserving_live_runtime_and_code(
    home, private
):
    if not sandbox.userns_available():
        pytest.skip("Unprivileged user/mount namespaces unavailable")
    workspace = home / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "code.py").write_text("original")
    broker_named_project = workspace / "mcp-gateway"
    broker_named_project.mkdir()
    (broker_named_project / "code.py").write_text("project code")
    memory = workspace / "memory"
    memory.mkdir()
    (memory / "preferences.md").write_text("GLOBAL PREFERENCES")
    (home / "memory.db").write_text("GLOBAL DATABASE")
    (home / "lessons.jsonl").write_text("GLOBAL LESSONS")
    (home / "config.json").write_text("{}")
    marker = home / ".private-member-runtime"
    marker.write_text("1")
    marker.chmod(0o400)
    (home / "tmpexisting.tmp").write_text("PRIVATE STAGING")
    (home / "home-alias").symlink_to(home, target_is_directory=True)
    (home / "temporary-alias").symlink_to(home / "tmpexisting.tmp")
    logs = home / "agent-logs"
    (logs / "member-other").mkdir(parents=True)
    (logs / "member-other" / "other.log").write_text("OTHER MEMBER LOG")
    hidden_logs = home / "memory_stores" / ".execution-logs" / "member-other"
    hidden_logs.mkdir(parents=True)
    (hidden_logs / "other.log").write_text("PRIVATE OTHER EXECUTION LOG")
    (home / "logs-alias").symlink_to(logs, target_is_directory=True)
    for name in ("backups", "run", "member-memory-bindings", "mcp-gateway"):
        (home / name).mkdir()
    (home / "broker-alias").symlink_to(home / "mcp-gateway", target_is_directory=True)
    (home / "backups" / "memory.old.db").write_text("GLOBAL BACKUP")
    child = home.parent / "probe.py"
    child.write_text(_CHILD)
    args = sandbox.namespace_argv(
        [sys.executable, str(child), "private" if private else "v1"],
        "standard",
        private_memory=private,
    )
    process = subprocess.Popen(
        args,
        cwd=workspace,
        text=True,
        encoding="utf-8",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "Namespace setup did not report readiness"
        ready = process.stdout.readline().strip()
        if ready != "READY":
            _, error = process.communicate(timeout=5)
            pytest.fail(f"Namespace setup failed: {ready} {error}")
        if private:
            from kiro_crew.member_memory_auth import publish_member_session_pid

            publish_member_session_pid(
                process.pid, "dashboard:filesystem-probe", home=home, memory_store="member-probe"
            )
        for name in ("memory.db", "memory.db-wal", "memory.db.superseded.future", "tmpfuture.tmp"):
            incoming = home / "incoming"
            incoming.write_text("LATE GLOBAL")
            incoming.replace(home / name)
        (home / "member-memory-bindings" / "late.txt").write_text("binding")
        (home / "run" / "gateway.secret").write_text("secret")
        (home / "mcp-gateway" / "late.txt").write_text("broker")
        (home / "kirocrew-mcp-gateway.sock").write_text("legacy broker canary")
        (home / "mc-mcp-gateway.sock").write_text("legacy broker canary")
        output, error = process.communicate("continue\n", timeout=20)
        assert process.returncode == 0, error
        result = json.loads(output)
        for name, value in result.items():
            assert value is (
                True
                if name
                in {
                    "late_binding",
                    "late_listener",
                    "code_read",
                    "broker_named_project",
                    "private_log",
                    "private_audit",
                    "hidden_logs_denied",
                    "diagnostic_route_valid",
                }
                else not private
            ), (name, result)
        assert (project / "new.py").read_text() == "member code"
        assert (broker_named_project / "new.py").read_text() == "member code"
        if private:
            assert (home / "memory.db").read_text() == "LATE GLOBAL"
            assert not (home / "security_events.jsonl").exists()
            assert not (home / "gateway.log").exists()
            execution_logs = [
                path
                for path in (home / "memory_stores" / ".execution-logs").iterdir()
                if path.name != "member-other"
            ]
            assert len(execution_logs) == 1
            assert list(execution_logs[0].glob("member-*.log"))
            assert list(execution_logs[0].glob("audit-*/security_events.jsonl"))
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
