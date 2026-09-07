"""Named memory stores: the two roots, the shape rule, and the resolvers.

A crew (``cfg.agents[<crew>].memory_store``) names a memory store, and a store
is a SEPARATE on-disk silo: its own markdown tree, its own FTS index and its
own vector-store SQLite file. There is no workspace column and no cutover — isolation is the
file boundary.

**There are TWO roots, and conflating them is the sharpest hazard here.** The
resolvers below answer for the same store name, and they answer with different
paths:

* :func:`memory_store_dir_for` — the MARKDOWN root, the directory holding
  ``memory/preferences.md``, ``memory/projects.md`` and ``memory/history/*.md``.
  For ``"default"`` this is :func:`kiro_crew.memory.workspace_dir`
  (``config_dir()/"workspace"``), NOT the data home: returning the data home
  would move every existing install's markdown memory out from under both the
  consolidator and ``kirocrew memory search``.
* :func:`resolve_store_path` — the VECTOR FILE. For ``"default"`` this is
  ``config_dir()/"memory.db"``, byte-identical to the path
  ``VectorMemoryStore()`` already defaults to.

:func:`memory_index_path_for` answers a third question and does NOT follow the
markdown root: the DEFAULT store's FTS index stays in the data-home root, beside
``memory.db``, because that is the only location the snapshot ``memory``
component, ``portability``'s export zip and ``scripts/sync-to-remote.sh`` name.
A NAMED store's index does live inside its own directory.

Nothing here moves data. A named store starts EMPTY; nothing is copied or
inferred from the default store.

A supplied named store is resolved exactly or raises UnknownMemoryStore. The
reserved default assistant retains Global Memory V1; every other Crew Member
requires its own owned V2 store, provisioned empty and never shared. Ownership
is recorded in config and in the store's bounded member-memory.json manifest.

LEAF module: stdlib-only imports at module scope, so ``security.py`` (imported
very early, and which needs :data:`MEMORY_STORES_DIR_NAME` to build its
sensitive-path fence) can depend on it without a cycle. Everything else is
imported inside the function that needs it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

#: Directory under the data home holding one subdirectory per NAMED store. The
#: default store is deliberately NOT under here — it keeps the pre-existing
#: ``workspace/`` tree and root ``memory.db``.
#:
#: Read+write fenced: this whole subtree is a keystone leaf in
#: ``security._CREW_SECRET_LEAVES``, so agent file tools can neither read nor
#: write another crew's memory. Legitimate readers open these paths DIRECTLY,
#: the established keystone-reader pattern.
MEMORY_STORES_DIR_NAME = "memory_stores"

#: The store name that is always resolvable. It is the FLOOR, in the sense
#: ``ACP_BACKEND_KIRO`` is the harness floor: it names the markdown tree and
#: vector file every existing install already has, so it counts as declared
#: whether or not the operator's ``memory_stores`` section mentions it. That is
#: what keeps a fresh install (no ``config.json`` at all) resolvable.
DEFAULT_MEMORY_STORE = "default"

#: Vector-store filename inside a store's own directory. Owned here because two
#: resolvers must spell it identically — this module's
#: :func:`resolve_store_path` and ``vector_memory``'s own default.
MEMORY_DB_FILE = "memory.db"

#: Longest usable store name. A store name becomes a single path segment, and a
#: 255-byte filesystem limit has to hold the name plus whatever a sidecar
#: appends to it, so the cap is well inside it rather than at it.
MEMORY_STORE_NAME_MAX = 80

# Same shape ``members._SLUG_RE`` enforces for member slugs — lowercase
# letters, digits and hyphens, no leading or trailing hyphen. Kept as a local
# constant rather than imported because it is private there, and because the
# two lists are allowed to diverge; the members store remains the source of
# truth for the spelling.
_STORE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

# Basenames Windows resolves to a DEVICE rather than a file, with or without an
# extension. Refused on every platform, not just Windows: a config written on
# Linux is carried to Windows, and a store whose directory cannot be created
# there is a silo that silently holds nothing.
_WINDOWS_RESERVED_BASENAMES: frozenset[str] = frozenset(
    {"con", "nul", "aux", "prn"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


class UnknownMemoryStore(ValueError):
    """A requested store cannot be used without losing its identity or ownership."""


class MemberAlreadyExists(UnknownMemoryStore):
    """A member creation lost a race with an existing config entry."""


def memory_store_name_defect(name: object) -> str | None:
    """Why *name* is unusable as a store name, or ``None`` when it is fine.

    The predicate half of :func:`validate_memory_store_name`, also used when
    reporting invalid config declarations without discarding the original data.

    Rules are ordered most-specific-first so the reported reason names the
    actual defect; :data:`_STORE_NAME_RE` is the final catch-all.
    """
    if not isinstance(name, str):
        return "not a string"
    if not name:
        return "empty"
    if len(name) > MEMORY_STORE_NAME_MAX:
        return f"longer than {MEMORY_STORE_NAME_MAX} characters"
    if name != name.lower():
        return "not lowercase"
    # A SINGLE path segment, checked against BOTH separators: ``a\\b`` is one
    # segment to ``posixpath`` and two to Windows, and the config is portable.
    if os.path.basename(name) != name or Path(name).name != name or "\\" in name:
        return "not a single path segment"
    if name in _WINDOWS_RESERVED_BASENAMES:
        return "a Windows reserved device basename"
    if name[-1] in ". ":
        return "ends with a dot or a space"
    if not _STORE_NAME_RE.match(name):
        return f"does not match {_STORE_NAME_RE.pattern}"
    return None


def memory_store_binding_defect(raw: object) -> str | None:
    """Why a submitted binding's shape is unusable, or ``None``.

    Empty strings remain accepted at the write boundary for old clients. New
    members are provisioned automatically; existing bindings are immutable and
    require ownership validation independently of this shape check. An empty
    legacy member binding does not authorize global memory at runtime.
    """
    if isinstance(raw, str) and not raw:
        return None
    return memory_store_name_defect(raw)


def named_store_or_empty(name: object) -> str:
    """*name* as a NAMED store, or ``""`` meaning the global store.

    The one definition of "this value means the global store". Six call sites
    across five modules spelled it themselves and two of them had already
    diverged: one stripped surrounding whitespace and one did not, so a
    hand-edited ``"  coding  "`` made the consolidator WRITE into the silo while
    the context builder READ the global store — a split-brain with no error on
    either side.

    Strips deliberately. A padded value cannot pass
    :func:`validate_memory_store_name` (a trailing space is a refusal, because a
    path segment ending in one is unusable on Windows), so the choice is between
    stripping here and answering two different things in two modules. Every
    write surface already rejects padding; this is what makes a config the
    validators never saw resolve the same way everywhere.

    Non-strings answer ``""``: metadata is read from disk and a caller must not
    have to type-check before asking.
    """
    if not isinstance(name, str):
        return ""
    stripped = name.strip()
    return "" if not stripped or stripped == DEFAULT_MEMORY_STORE else stripped


def validate_memory_store_name(name: str) -> str:
    """Return *name* unchanged when it is a usable store name, else raise.

    Applied before path composition, including when resolving config bindings.
    The pattern admits no ``/``, ``\\``, ``.`` or whitespace, so a validated name
    cannot traverse out of :func:`memory_stores_root` on its own; the resolvers
    still re-check containment after composition, because validation and use
    are separated by a call boundary a future caller could bypass (the same
    pairing ``members.validate_slug`` / ``members.member_dir`` uses).
    """
    defect = memory_store_name_defect(name)
    if defect is not None:
        raise UnknownMemoryStore(f"invalid memory store name {name!r}: {defect}")
    return name


def memory_stores_root() -> Path:
    """The directory holding one subdirectory per NAMED memory store."""
    from kiro_crew.config.loader import config_dir

    return config_dir() / MEMORY_STORES_DIR_NAME


def usable_store_names(declared: Iterable[str]) -> frozenset[str]:
    """The subset of *declared* that can actually become a store on disk.

    A MALFORMED name is undeclared for resolution even though
    ``KiroCrewConfig.load`` keeps the operator's entry verbatim — reporting a
    defect must not erase a line the operator wrote, and a name no resolver will
    compose a path for is still not a store any crew can run on. Both membership
    tests in the tree run through this filter (:func:`_declared_stores` here,
    ``config.loader.resolve_agent_bindings`` for a crew's binding), which is what
    keeps them from disagreeing: a raw-table test would hand a crew a name that
    :func:`validate_memory_store_name` then refuses at the first memory write.

    Filtering ONLY — :data:`DEFAULT_MEMORY_STORE` is not added here. The floor
    belongs to the resolvers, which must answer for it on an install with no
    ``config.json`` at all; a crew's binding reads a loaded table that already
    carries a synthesized default entry whenever the section was empty, so
    adding the floor there would instead change which store an existing config's
    crew lands on.

    Pure — no config load — so ``resolve_agent_bindings`` can call it with the
    config already in hand instead of re-entering the loader.
    """
    return frozenset(n for n in declared if memory_store_name_defect(n) is None)


#: ``(config fingerprint, declared names, configured default)`` for the last
#: resolution. Not an LRU: there is exactly one config, so one slot is the whole
#: cache, and keying on the fingerprint means a stale entry is impossible rather
#: than merely unlikely.
_DECLARED_MEMO: tuple[object, frozenset[str], str] | None = None


def _set_declared_memo(fp: object, declared: frozenset[str], configured_default: str) -> None:
    global _DECLARED_MEMO
    _DECLARED_MEMO = (fp, declared, configured_default)


def _declared_stores() -> tuple[frozenset[str], str]:
    """``(resolvable store names, cfg.default_memory_store)`` off the LOADED config.

    Reads ``KiroCrewConfig.load()``, never ``_raw_config()``. The raw dict is
    the bytes on disk: it carries no ``memory_stores`` key until a write-back
    migration adds one, and that migration is SKIPPED whenever the load
    degraded a section — so a raw-dict resolver reports ``"default"`` as
    undeclared on a fresh install, and keeps reporting it on any install with a
    malformed config section. The loaded config synthesizes the default entry.

    :data:`DEFAULT_MEMORY_STORE` is unioned in unconditionally: it is the floor,
    naming the markdown tree and vector file every install already has, so it
    stays resolvable even when the config cannot be read at all.

    Never raises — a config that will not load degrades to the floor alone.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig, _config_fingerprint

        # Memoized on the loader's OWN change-detector, so it invalidates exactly
        # when the config does. A full ``KiroCrewConfig.load()`` deep-copies the
        # cached dict and rebuilds every dataclass to answer for two fields, and
        # resolution runs several times per turn on a named store; keying on the
        # fingerprint turns that into a stat.
        fp = _config_fingerprint()
        memo = _DECLARED_MEMO
        if memo is not None and memo[0] == fp:
            return memo[1], memo[2]
        cfg = KiroCrewConfig.load()
        declared = usable_store_names(cfg.memory_stores) | {DEFAULT_MEMORY_STORE}
        _set_declared_memo(fp, declared, cfg.default_memory_store)
        return declared, cfg.default_memory_store
    except Exception:
        logger.warning(
            "could not load config to enumerate memory stores; using the %r store only",
            DEFAULT_MEMORY_STORE,
            exc_info=True,
        )
        return frozenset({DEFAULT_MEMORY_STORE}), DEFAULT_MEMORY_STORE


def resolve_declared_store(store: str) -> str:
    """Resolve the exact declared store; a supplied name never falls back to V1."""
    validate_memory_store_name(store)
    if store == DEFAULT_MEMORY_STORE:
        return store
    declared, _ = _declared_stores()
    if store not in declared:
        raise UnknownMemoryStore(
            f"memory store {store!r} is not declared; global memory was not used"
        )
    return store


def _named_store_dir(name: str) -> Path:
    """Compose a NAMED store's directory and re-check containment.

    *name* must already be shape-validated and must not be
    :data:`DEFAULT_MEMORY_STORE`.
    """
    root = memory_stores_root().resolve()
    expected = root / name
    target = expected.resolve()
    # Defence in depth behind validate_memory_store_name, mirroring
    # members.member_dir: a symlinked component must not redirect a store.
    #
    # The test is IDENTITY, not containment, and the difference is a real
    # isolation hole rather than a hypothetical one. Checking only
    # ``target.parent == root`` refuses a link that escapes the root and ACCEPTS
    # one that redirects INSIDE it: with ``memory_stores/acme`` pointing at
    # ``memory_stores/finance``, the resolved parent is still the root, so both
    # crews were handed one silo -- vector rows, markdown and lessons -- with
    # every path check reporting success. Requiring the resolved path to be the
    # one that was composed refuses the redirect and still admits a root reached
    # through a symlinked ancestor (``/tmp`` on macOS), because ``root`` is
    # resolved before the join.
    #
    # A non-existent store resolves to itself (``strict=False``), so a store
    # being created for the first time passes.
    if target != expected:
        raise UnknownMemoryStore(
            f"memory store {name!r} resolves to {target}, not {expected}; refusing a "
            f"link that would share another store's directory"
        )
    return target


def memory_store_dir_for(store: str) -> Path:
    """The MARKDOWN root for *store*. Does not create anything.

    ``"default"`` resolves to ``memory.workspace_dir()``
    (``config_dir()/"workspace"``) so no existing install's ``preferences.md``,
    ``projects.md`` or ``history/`` moves. A declared name resolves to
    ``config_dir()/memory_stores/<name>``.

    NOT the home of the FTS index for every store — the default store's index
    sits in the data-home root instead. :func:`memory_index_path_for` owns that.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.memory import workspace_dir

        return workspace_dir()
    return _named_store_dir(name)


def memory_index_path_for(store: str) -> Path:
    """The FTS5 index file for *store*. Does not create anything.

    ``"default"`` resolves to ``config_dir()/memory_index.db``, the data-home
    root — where the index of every existing install already sits, and the only
    place the off-store consumers look for it: the snapshot ``memory``
    component's ``files`` tuple, ``portability``'s export/import zip and
    ``scripts/sync-to-remote.sh`` all name it root-relative. So this is
    deliberately NOT ``memory_store_dir_for(store)``'s answer for the default
    store; moving it there would silently drop the index from every backup while
    a restore wrote a copy nothing reads.

    A NAMED store's index lives inside that store's own directory, beside the
    markdown tree it describes, which is what makes the index per-store and puts
    it behind the ``memory_stores/`` fence. Whichever step gives the off-store
    consumers a per-store view owns extending them; until then a named store's
    index is simply outside their reach.

    The index is fully DERIVED — ``MemoryStore.rebuild_index`` regenerates it
    from preferences.md, projects.md and history/*.md and reads no index state —
    so a store whose index is not backed up loses search results until the next
    rebuild, never memory.
    """
    from kiro_crew.memory import INDEX_DB_FILE

    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.config.loader import config_dir

        return config_dir() / INDEX_DB_FILE
    return _named_store_dir(name) / INDEX_DB_FILE


def resolve_store_path(store: str) -> Path:
    """The VECTOR FILE (semantic/episodic/lessons SQLite) for *store*.

    ``"default"`` resolves to ``config_dir()/"memory.db"`` — byte-exact with
    ``VectorMemoryStore()``'s own default, so the default store keeps the file
    it already has. A named store gets ``memory.db`` inside its own directory,
    which is also what scopes ``VectorMemoryStore.init``'s owner-only
    tightening of ``db_path.parent`` to that store.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.config.loader import config_dir

        return config_dir() / MEMORY_DB_FILE
    return _named_store_dir(name) / MEMORY_DB_FILE


def named_store_of_db(path: Path) -> str:
    """The NAMED store whose vector file is *path*, or ``""`` when it is not one.

    The inverse of :func:`resolve_store_path`, and the only POSITIVE spelling of
    "this file is a crew silo". Answers ``""`` for the default store, for an eval
    or import destination, for a bare temp path, and for anything malformed —
    which is the whole point. The negation a caller would otherwise reach for,
    ``path != config_dir()/"memory.db"``, is true of four real non-silo paths
    (the eval runner's ``ws/"vector_memory.db"``, the bench ingest path, the
    onboarding importer's ``destination/"memory.db"``, and every ``tmp_path`` in
    the suite), so it would hand each of them silo treatment.

    The containment test is IDENTITY, for the reason spelled out in
    :func:`_named_store_dir`: with ``memory_stores/acme`` symlinked at
    ``memory_stores/finance``, a resolved-parent check still sees the root and
    would answer ``"acme"`` for a file that physically belongs to ``finance`` —
    naming the alias rather than the store, which is the same aliasing hole the
    forward direction already refuses.
    """
    if path.name != MEMORY_DB_FILE:
        return ""
    parent = path.parent
    name = parent.name
    # ``named_store_or_empty`` rather than a bare shape check, so the literal
    # ``memory_stores/default/`` answers "" here too. That directory is
    # unreachable through ``resolve_store_path`` (which maps the name to the
    # data-home root before composing a path), but a caller handing this function
    # an arbitrary path must not be told the name of the GLOBAL store.
    if named_store_or_empty(name) != name or memory_store_name_defect(name) is not None:
        return ""
    try:
        root = memory_stores_root().resolve()
        if parent.resolve() != root / name:
            return ""
    except OSError:
        return ""
    return name


def declared_store_names() -> list[str]:
    """Every store name a whole-install pass covers: the DEFAULT store first, then the rest.

    ONE enumeration, because a pass that builds its own is a pass that can disagree with
    another about which stores exist -- and a store missing from one of them is a store
    whose contents that pass reports nothing about while still printing a verdict.

    Names come off the operator's DECLARED table through :func:`usable_store_names`, the
    one filter every membership test runs through. A directory listing of
    ``memory_stores/`` is deliberately NOT used: it would adopt a silo the config no
    longer declares, or one a restore dropped in, and then treat it as the operator's.

    DEFAULT FIRST, then sorted -- not sorted overall. The default store is the one every
    install has, so it leads every report; a plain sort buries it wherever the alphabet
    puts it and makes two passes over the same install list in different orders.

    Never raises. A config that cannot be read degrades to the default store alone, the
    same floor :func:`_declared_stores` falls back to.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        declared = usable_store_names(KiroCrewConfig.load().memory_stores)
    except Exception:
        logger.warning(
            "could not enumerate declared memory stores; using %r alone",
            DEFAULT_MEMORY_STORE,
            exc_info=True,
        )
        return [DEFAULT_MEMORY_STORE]
    return [DEFAULT_MEMORY_STORE, *sorted(declared - {DEFAULT_MEMORY_STORE})]


def owned_store_path(store: str) -> Path | None:
    """*store*'s vector file, or ``None`` when the resolution does not belong to it.

    Whole-install maintenance can skip an unavailable store while continuing
    with the others. Runtime member resolution uses the raising ownership
    validators instead; this helper must never choose a replacement store.
    """
    try:
        path = resolve_store_path(store)
    except Exception:
        logger.warning("memory store %r has no resolvable vector file", store, exc_info=True)
        return None
    if named_store_or_empty(store) and named_store_of_db(path) != store:
        logger.warning(
            "memory store %r resolved to %s, which is not that store's own file", store, path
        )
        return None
    return path


def ensure_memory_store_dir(store: str) -> Path:
    """Create *store*'s markdown root owner-only and return it.

    The stores ROOT is created and tightened before its first child exists,
    which is what the Windows half depends on: ``restrict_dir_to_owner``'s
    grants carry ``(OI)(CI)``, so a store directory created inside an
    already-tightened root inherits owner-only access instead of landing on the
    creating token's default DACL.

    ``"default"`` is returned untouched: its root is the pre-existing
    ``workspace/`` tree, created and owned by ``MemoryStore.init()``, and
    creating or tightening it from here would change the default path.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.memory import workspace_dir

        return workspace_dir()
    from kiro_crew import platform_compat

    platform_compat.make_owner_only_dir(memory_stores_root())
    target = _named_store_dir(name)
    platform_compat.make_owner_only_dir(target)
    return target


MEMBER_MEMORY_MANIFEST = "member-memory.json"


def _member_manifest(name: str) -> dict:
    """Read a bounded ownership record without following a substituted file."""
    target = _named_store_dir(validate_memory_store_name(name))
    manifest = target / MEMBER_MEMORY_MANIFEST
    try:
        if manifest.resolve() != manifest or manifest.stat().st_size > 4096:
            raise UnknownMemoryStore(f"memory store {name!r} has an invalid ownership record")
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("ownership record must be an object")
        return value
    except (OSError, ValueError) as exc:
        raise UnknownMemoryStore(
            f"memory store {name!r} ownership record is missing or unreadable: {exc}"
        ) from exc


def memory_store_version(store: str, *, config=None) -> int:
    """Identify V2 positively; the global store and unowned legacy stores are V1.

    Without a config this reads only the bounded ownership manifest. It is safe
    during vector-store initialization and does not recursively load config.
    Runtime authorization must still use ``require_member_memory_store``.
    """
    if store == DEFAULT_MEMORY_STORE or memory_store_name_defect(store) is not None:
        return 1
    if config is not None:
        record = config.memory_stores.get(store)
        if not record or record.memory_version != 2 or not record.owner_member:
            return 1
    try:
        manifest = _member_manifest(store)
    except UnknownMemoryStore:
        return 1
    return 2 if manifest.get("memory_version") == 2 and manifest.get("owner_member") else 1


def require_memory_store(store: str, *, config=None, require_directory: bool = True) -> str:
    """Validate a trusted persisted store binding, without fallback or repair.

    ``default`` is the explicit V1 identity. Callers must distinguish absent
    legacy metadata from malformed or missing member bindings before calling.
    Directory existence is required on use, so deleting a member directory
    cannot silently replace its memory with an empty store.
    """
    validate_memory_store_name(store)
    if store == DEFAULT_MEMORY_STORE:
        return store
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    if store not in usable_store_names(config.memory_stores):
        raise UnknownMemoryStore(
            f"memory store {store!r} is not declared; global memory was not used"
        )
    if require_directory:
        target = _named_store_dir(store)
        try:
            # scandir actually opens the directory, unlike exists()/os.access().
            with os.scandir(target):
                pass
        except OSError as exc:
            raise UnknownMemoryStore(
                f"memory store {store!r} is missing or unreadable: {exc}; global memory was not used"
            ) from exc
    record = config.memory_stores[store]
    owner = getattr(record, "owner_member", "")
    if owner or getattr(record, "memory_version", 1) == 2:
        if not isinstance(owner, str) or not owner or owner == DEFAULT_MEMORY_STORE:
            raise UnknownMemoryStore(f"memory store {store!r} has no valid private owner")
        if getattr(record, "memory_version", 1) != 2:
            raise UnknownMemoryStore(f"memory store {store!r} has inconsistent memory version")
        bindings = [name for name, agent in config.agents.items() if agent.memory_store == store]
        if bindings != [owner]:
            raise UnknownMemoryStore(
                f"memory store {store!r} must belong exclusively to member {owner!r}"
            )
        if require_directory:
            manifest = _member_manifest(store)
            if manifest.get("owner_member") != owner or manifest.get("memory_version") != 2:
                raise UnknownMemoryStore(
                    f"memory store {store!r} ownership does not match member {owner!r}"
                )
            database = target / MEMORY_DB_FILE
            try:
                if database.resolve() != database:
                    raise OSError("database is redirected to another file")
                with database.open("rb") as handle:
                    if handle.read(16) != b"SQLite format 3\x00":
                        raise OSError("database header is invalid")
            except OSError as exc:
                raise UnknownMemoryStore(
                    f"memory store {store!r} database is missing or unreadable: {exc}"
                ) from exc
    return store


def require_member_memory_store(config, member: str, *, require_directory: bool = True) -> str:
    """The sole private V2 store of a member, or the default assistant's V1."""
    if member == DEFAULT_MEMORY_STORE:
        return DEFAULT_MEMORY_STORE
    agent = config.agents.get(member)
    if agent is None:
        raise UnknownMemoryStore(f"unknown Crew Member {member!r}; global memory was not used")
    store = agent.memory_store
    record = config.memory_stores.get(store) if isinstance(store, str) else None
    if (
        not isinstance(store, str)
        or store == DEFAULT_MEMORY_STORE
        or record is None
        or getattr(record, "owner_member", "") != member
        or getattr(record, "memory_version", 1) != 2
    ):
        raise UnknownMemoryStore(
            f"Crew Member {member!r} needs its own V2 memory. Initialize private memory "
            "in the member settings; existing memory will not be copied or modified."
        )
    return require_memory_store(store, config=config, require_directory=require_directory)


def provision_member_memory(config, member: str) -> str:
    """Allocate an empty private V2 store and bind the member in the given config.

    The caller holds its config mutation lock and persists the record before
    exposing the member. An exclusive random directory claim and ownership
    manifest prevent another process or a recreated member adopting old data.
    Existing private ownership is immutable; this is only creation/explicit
    initialization of legacy members, never a reset operation.
    """
    from kiro_crew import platform_compat
    from kiro_crew.config.sections import MemoryStoreConfig

    if member == DEFAULT_MEMORY_STORE:
        raise UnknownMemoryStore("the default assistant keeps Global Memory V1")
    if member not in config.agents:
        raise UnknownMemoryStore(f"unknown Crew Member {member!r}")
    agent = config.agents[member]
    current = (
        config.memory_stores.get(agent.memory_store)
        if isinstance(agent.memory_store, str)
        else None
    )
    if current and getattr(current, "owner_member", ""):
        return require_member_memory_store(config, member)
    # Never grant two private stores to the same member, including a stale
    # binding or deleted member recreated under its previous display name.
    if any(
        getattr(record, "owner_member", "") == member for record in config.memory_stores.values()
    ):
        raise UnknownMemoryStore(
            f"Crew Member {member!r} already owns private memory; restore its binding"
        )
    root = memory_stores_root()
    platform_compat.make_owner_only_dir(root)
    slug = re.sub(r"[^a-z0-9]+", "-", member.lower()).strip("-")[:32] or "crew"
    while True:
        name = f"member-{slug}-{uuid.uuid4().hex}"
        if name in config.memory_stores:
            continue
        target = _named_store_dir(name)
        try:
            target.mkdir(mode=0o700, exist_ok=False)
            break
        except FileExistsError:
            continue
    manifest = target / MEMBER_MEMORY_MANIFEST
    try:
        platform_compat.make_owner_only_dir(target)
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump({"owner_member": member, "memory_version": 2}, handle)
        # Create the canonical V2 database before the member becomes visible.
        # init() selects algorithms from the manifest without loading config.
        from kiro_crew.vector_memory import VectorMemoryStore

        vectors = VectorMemoryStore(db_path=target / MEMORY_DB_FILE)
        try:
            vectors.init()
        finally:
            vectors.close()
        config.memory_stores[name] = MemoryStoreConfig(owner_member=member, memory_version=2)
        agent.memory_store = name
    except BaseException:
        # Only the just-created manifest and an empty directory are removed.
        # Never recursively clean a path which another component may have used.
        manifest.unlink(missing_ok=True)
        try:
            target.rmdir()
        except OSError:
            logger.warning(
                "failed member initialization left an unreferenced directory at %s", target
            )
        raise
    return name


def persist_member_config(
    config,
    member: str,
    *,
    create: bool = False,
    expected_store=None,
    changed_fields: set[str] | None = None,
) -> None:
    """Atomically publish a member and its ownership while retaining other writes.

    Competing creates/initializations of the same member are refused under the
    cross-process config lock. A losing writer can leave an unreferenced empty
    store, but can neither replace the winner nor adopt another store.

    Updates may name only the fields the caller actually changed, preserving
    concurrent edits to other fields. None retains full-record publication;
    creation always publishes the full record. A new binding must be included.
    """
    from dataclasses import asdict

    from kiro_crew.config.loader import _invalidate_config_cache, update_config_locked

    unchanged_binding = not create and config.agents[member].memory_store == expected_store
    store = (
        config.agents[member].memory_store
        if unchanged_binding
        else require_member_memory_store(config, member)
    )
    agent_record = asdict(config.agents[member])
    if changed_fields is not None:
        if changed_fields - agent_record.keys():
            raise UnknownMemoryStore("member update contains unknown fields")
        if not create and not unchanged_binding and "memory_store" not in changed_fields:
            raise UnknownMemoryStore("member update omitted its changed memory binding")
        if not create:
            agent_record = {
                key: value for key, value in agent_record.items() if key in changed_fields
            }
    store_record = (
        asdict(config.memory_stores[store])
        if not unchanged_binding and store != DEFAULT_MEMORY_STORE
        else None
    )

    def mutate(data: dict) -> dict:
        agents = data.setdefault("agents", {})
        stores = data.setdefault("memory_stores", {})
        if not isinstance(agents, dict) or not isinstance(stores, dict):
            raise UnknownMemoryStore("agent or memory store configuration is unreadable")
        if DEFAULT_MEMORY_STORE not in agents and DEFAULT_MEMORY_STORE in config.agents:
            agents[DEFAULT_MEMORY_STORE] = asdict(config.agents[DEFAULT_MEMORY_STORE])
        current = agents.get(member)
        if create and member in agents:
            raise MemberAlreadyExists(
                f"Crew Member {member!r} was created concurrently; reload the roster"
            )
        if not create and current is None:
            raise UnknownMemoryStore(
                f"Crew Member {member!r} was removed concurrently; reload the roster"
            )
        if not create and current is not None:
            if (
                not isinstance(current, dict)
                or current.get("memory_store", DEFAULT_MEMORY_STORE) != expected_store
            ):
                raise UnknownMemoryStore(
                    f"Crew Member {member!r} memory changed concurrently; reload the roster"
                )
        if store_record is not None:
            for name, entry in stores.items():
                if (
                    isinstance(entry, dict)
                    and entry.get("owner_member") == member
                    and name != store
                ):
                    raise UnknownMemoryStore(
                        f"Crew Member {member!r} already owns another private store"
                    )
            for name, entry in agents.items():
                if (
                    name != member
                    and isinstance(entry, dict)
                    and entry.get("memory_store") == store
                ):
                    raise UnknownMemoryStore(
                        f"memory store {store!r} is already bound to another member"
                    )
            existing = stores.get(store)
            if existing is not None and (
                not isinstance(existing, dict) or existing.get("owner_member") != member
            ):
                raise UnknownMemoryStore(f"memory store {store!r} ownership changed concurrently")
            stores[store] = {**(existing or {}), **store_record}
        agents[member] = {**(current or {}), **agent_record}
        return data

    update_config_locked(mutate=mutate)
    _invalidate_config_cache()
