"""Short added/removed line excerpt between two snapshots."""

from __future__ import annotations

import difflib

MAX_LINES = 5
MAX_CHARS = 200


def make_excerpt(old: str, new: str) -> dict[str, list[str]]:
    added: list[str] = []
    removed: list[str] = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added.append(line[2:])
        elif line.startswith("- "):
            removed.append(line[2:])
    return {
        "added": [x[:MAX_CHARS] for x in added[:MAX_LINES]],
        "removed": [x[:MAX_CHARS] for x in removed[:MAX_LINES]],
    }
