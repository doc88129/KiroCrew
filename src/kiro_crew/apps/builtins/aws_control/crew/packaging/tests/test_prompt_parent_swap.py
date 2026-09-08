"""A swapped PARENT directory must not be traversed on the way to a prompt.

``O_NOFOLLOW`` on a single open refuses a FINAL-component link only. The agents
directory is writable, so an agent can leave the leaf name alone and swap a parent for
a link to ``~/.ssh`` instead: the final component is then a real file, the single-open
check passes, and the prompt that ships inside ``agent.json`` carries the target's
bytes. Measured before the fix -- a parent swap read private key material straight
through.
"""

from __future__ import annotations

import os
import pathlib

import pytest

pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"),
    reason="the per-component opener needs dir_fd; the single-open fallback is a "
    "documented narrowing on platforms without it",
)


def _tree(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    root = tmp_path / "agents"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "prompt.md").write_text("PRIVATE-KEY-MATERIAL\n")
    (root / "sub" / "prompt.md").write_text("a harmless prompt\n")
    return root, root / "sub" / "prompt.md"
