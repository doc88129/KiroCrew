"""Two findings on the prompt reader, both raised by GPT and Opus independently.

V1 the pre-resolution walk ran only on the RELATIVE branch, so ``file:///abs/path`` reached
   ``resolve()`` with nothing having looked at its components. What that costs is Windows-only
   and narrow: resolving a reparse point that names a SHARE is the outbound SMB probe with its
   NTLM exchange, and the UNC gate above only sees a share written literally in the target.

   The first attempt walked the absolute path and refused every redirect. That reddened
   ``test_a_symlink_to_a_legitimate_persona_still_works`` and four more, because a symlink at
   the prompt path is a SUPPORTED case -- the design permits a persona outside the agents
   directory and protects it by checking the RESOLVED target against the sensitive-path fence.
   So the refusal is scoped to the one thing the target check cannot catch: a redirect that
   names a share, read with ``readlink``, which does not traverse.

V2 the read was unbounded. The prompt is inlined into ``agent.json``, so its bytes are held in
   memory, hashed and shipped -- and the path comes from the crew's agent spec, which makes the
   size someone else's choice.
"""

from __future__ import annotations

import importlib
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew


# ---------------------------------------------------------------------------
# V1
# ---------------------------------------------------------------------------
def test_a_symlinked_persona_outside_the_agents_dir_still_works(tmp_path: pathlib.Path) -> None:
    """The supported case, pinned again here because a fix already broke it once.

    Kept beside the new refusal rather than left in its own file: the two are one decision, and
    a reader deciding whether to tighten the absolute branch needs to see the cost in the same
    place as the benefit.
    """
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    persona = tmp_path / "shared" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_bytes(b"You are the front desk.\n")
    link = agents_dir / "linked.md"
    link.symlink_to(persona)

    assert mod._resolve_prompt_path(f"file://{link}", agents_dir) == link


def test_the_absolute_branch_refuses_a_redirect_naming_a_share(tmp_path: pathlib.Path) -> None:
    """The gap the walk was added for, tested through the nt branch.

    ``os.name`` is mutated rather than the test being skipped, because the branch cannot be
    reached on this host and skipping it would leave the fix with no test at all -- which is
    how the previous version of this fence shipped ineffective.
    """
    mod = load_build(mutate=('    elif os.name == "nt":', "    elif True:"))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # A link whose TARGET has UNC shape. The target need not exist: refusing before resolution
    # is the point, and a dangling link proves nothing was resolved.
    link = agents_dir / "persona.md"
    link.symlink_to("//attacker-host/share/persona.md")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{link}", agents_dir)
    assert "network share" in str(caught.value)
    assert "attacker-host" in str(caught.value)


def test_an_ordinary_absolute_link_is_not_refused_by_that_check(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: on the same branch, a link to a local file must pass.

    Without this the refusal above would be satisfied by banning every link, which is exactly
    the over-broad version that had to be backed out.
    """
    mod = load_build(mutate=('    elif os.name == "nt":', "    elif True:"))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    persona = tmp_path / "shared" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_bytes(b"a local persona\n")
    link = agents_dir / "persona.md"
    link.symlink_to(persona)

    assert mod._resolve_prompt_path(f"file://{link}", agents_dir) == link


# ---------------------------------------------------------------------------
# V2
# ---------------------------------------------------------------------------
def test_an_oversized_prompt_is_refused(tmp_path: pathlib.Path) -> None:
    """Refused rather than read, because the read is what allocates."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    big = home / "agents" / "persona.md"
    big.write_bytes(b"x" * (mod._MAX_PROMPT_BYTES + 1))

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "exceeds the" in str(caught.value)


def test_a_prompt_at_the_ceiling_is_read(tmp_path: pathlib.Path) -> None:
    """The bound is inclusive, so the ceiling is a size and not an off-by-one."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    body = b"y" * mod._MAX_PROMPT_BYTES
    (home / "agents" / "persona.md").write_bytes(body)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["prompt"] == body.decode("utf-8")


def test_the_bound_is_handed_to_the_shared_guard(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The ceiling reaches the guard as ``max_bytes``, and its refusal becomes ExportRefused.

    ``hooks.safe_read_file_bytes_nolink`` owns the bound, so this module's part is the value
    it hands over, and that value is the observable thing here. It is worth observing because
    handing over no bound leaves every behavioural test green while the allocation runs
    unbounded: the prompt is inlined into ``agent.json``, so its bytes are held in memory and
    hashed, and the size is named by the crew's agent spec rather than by this code.

    Recorded rather than re-derived: the value asserted is the one the call actually carried.
    """
    mod = load_build()
    # Patched on kiro_crew.hooks, not on the module under test: the import is INSIDE the
    # function (it has to be, so a missing hooks module fails closed there), so the name is
    # never a module attribute and patching the module under test silently observes nothing.
    hooks = importlib.import_module("kiro_crew.hooks")
    seen: list[object] = []
    real = hooks.safe_read_file_bytes_nolink

    def _recording(path, anchor, **kwargs):
        seen.append(kwargs.get("max_bytes"))
        return real(path, anchor, **kwargs)

    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", _recording)

    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    (home / "agents" / "persona.md").write_text("a small persona\n", encoding="utf-8")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)

    assert seen, "the prompt read did not go through the shared guard, so this proves nothing"
    assert seen == [mod._MAX_PROMPT_BYTES], f"the bound handed over was {seen}"


def test_an_ordinary_prompt_is_unaffected_by_the_ceiling(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a persona is prose, and prose must still read verbatim."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    (home / "agents" / "persona.md").write_bytes(b"You are the front desk.\nBe brief.\n")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["prompt"] == "You are the front desk.\nBe brief.\n"


@pytest.mark.parametrize(
    "label,raw",
    [
        ("relative", "file://per\x00sona.md"),
        ("bare", "file://\x00"),
        ("absolute", "file:///tmp/per\x00sona.md"),
    ],
)
def test_a_nul_in_the_reference_is_refused_not_raised(
    tmp_path: pathlib.Path, label: str, raw: str
) -> None:
    """A NUL reaches a syscall as a bare ValueError, so it is refused on the string.

    The target comes from the crew's agent spec, which makes its bytes someone else's choice.
    Measured before the fix: all three of these left ValueError uncaught and it reached the
    CLI as a traceback, naming neither the spec nor the reference.

    All three shapes are covered because the branches differ -- relative and absolute take
    different paths through this function, and a NUL alone leaves an otherwise empty target.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(raw, agents)
    assert "NUL" in str(caught.value)


def test_MUTATION_without_the_nul_guard_the_reference_raises_valueerror(
    tmp_path: pathlib.Path,
) -> None:
    """Removing the guard must restore the bare ValueError, or the test above proves nothing."""
    mod = load_build(mutate=('    if "\\x00" in target:', "    if False:"))
    agents = tmp_path / "agents"
    agents.mkdir()
    with pytest.raises(ValueError) as caught:
        mod._resolve_prompt_path("file://per\x00sona.md", agents)
    assert not isinstance(caught.value, mod.ExportRefused), (
        "mutated to skip the NUL guard: the refusal still came back as ExportRefused, so the "
        "guard under test is not what produces it"
    )


@pytest.mark.parametrize("label,raw", [("relative", "file://persona.md"), ("absolute", None)])
def test_a_cycle_at_the_agents_directory_is_refused_not_raised(
    tmp_path: pathlib.Path, label: str, raw: str | None
) -> None:
    """A link loop AT the agents directory must refuse, on both ways in.

    The anchor is resolved before either branch chooses a path, so a cycle there is reached by
    a relative and an absolute reference alike. ``resolve()`` reports a loop as
    ``OSError(ELOOP)`` on some libcs and ``RuntimeError`` on others; measured on this host it
    was ``RuntimeError``, which is why catching only ``OSError`` left it escaping as a
    traceback that named neither the crew nor the reference.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    other = tmp_path / "other"
    agents.symlink_to(other)
    other.symlink_to(agents)

    reference = raw if raw is not None else f"file://{tmp_path}/agents/persona.md"
    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(reference, agents)
    assert "cannot be resolved" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make a real file uninspectable")
def test_an_uninspectable_agent_spec_is_not_reported_as_missing(tmp_path: pathlib.Path) -> None:
    """Present-but-uninspectable and absent are different facts and get different refusals.

    Reporting the first as "nothing to deploy" sends the operator to look for a missing file
    while the spec sits there unreadable. The distinction is made where the ``lstat`` fails, so
    each outcome is refused at the point that detects it.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    crew = mod.resolve_crew("frontdesk", home)
    parent = crew.agent_spec_path.parent
    os.chmod(parent, 0o000)
    try:
        with pytest.raises(mod.ExportRefused) as caught:
            mod.read_agent_spec(crew)
    finally:
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(parent, 0o755)
    message = str(caught.value)
    assert "could not be inspected" in message, message
    assert "There is nothing to deploy" not in message, (
        "an unreadable spec was reported as absent, which sends the operator after a file "
        "that is present"
    )


@pytest.mark.skipif(os.name != "posix", reason="plants a symlink to open the redirect walk")
def test_a_hop_that_cannot_be_read_refuses_instead_of_ending_the_walk(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A hop that exists and cannot be read must refuse, not quietly end the walk.

    ``readlink`` answers EINVAL for an ordinary file, which is how the walk ends normally. Any
    other error means the hop is there and could not be judged, and ending the walk on it lets
    the ``resolve()`` below traverse a hop nothing looked at -- which on Windows is the SMB
    probe this walk exists to prevent.

    Two things are simulated and neither is the behaviour under test. The walk lives in the
    ``os.name == "nt"`` branch, opened with the mutation this suite already uses. And the
    failure is INJECTED: reaching it needs a hop whose ``lstat`` succeeds while its
    ``readlink`` fails, and on POSIX both need the same parent traverse permission, so no file
    layout here produces it. What is asserted is what the handler does with the error.
    """
    mod = load_build(mutate=('    elif os.name == "nt":', "    elif True:"))
    agents = tmp_path / "home" / "agents"
    agents.mkdir(parents=True)
    target = agents / "real.md"
    target.write_text("a persona\n", encoding="utf-8")
    link = agents / "persona.md"
    link.symlink_to(target)

    real_readlink = mod.os.readlink

    def _readlink(path, *a, **k):
        if str(path).endswith("persona.md"):
            raise PermissionError(13, "Permission denied")
        return real_readlink(path, *a, **k)

    monkeypatch.setattr(mod.os, "readlink", _readlink)

    # ABSOLUTE, because the hop walk is on that branch: a relative reference is judged by the
    # component walk above it, which refuses the link before this code is reached.
    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{link}", agents)
    assert "could not be inspected" in str(caught.value), str(caught.value)
