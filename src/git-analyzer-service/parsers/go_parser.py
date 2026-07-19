"""
parsers/go_parser.py

Detects Go dependencies from go.mod files.
Parses the `require` block (both single and grouped).
"""
import re
from pathlib import Path
from typing import List


_SINGLE_REQUIRE = re.compile(
    r"^require\s+([\w.\-/]+)\s+([\w.\-+]+)", re.MULTILINE
)
_BLOCK_REQUIRE = re.compile(
    r"require\s*\(([^)]+)\)", re.DOTALL
)
_BLOCK_LINE = re.compile(r"^\s*([\w.\-/]+)\s+([\w.\-+]+)", re.MULTILINE)


def parse_go_mod(content: str) -> List[dict]:
    """Parse go.mod content into dependency list."""
    deps = []

    # Single-line requires
    for m in _SINGLE_REQUIRE.finditer(content):
        deps.append({"name": m.group(1), "version": m.group(2),
                     "ecosystem": "go", "sourceFile": "go.mod"})

    # Grouped require blocks
    for block in _BLOCK_REQUIRE.finditer(content):
        for m in _BLOCK_LINE.finditer(block.group(1)):
            name, version = m.group(1), m.group(2)
            # Skip indirect marker lines (they are comments, not packages)
            if name.startswith("//"):
                continue
            deps.append({"name": name, "version": version,
                         "ecosystem": "go", "sourceFile": "go.mod"})

    # Deduplicate by (name, version)
    seen = set()
    unique = []
    for d in deps:
        key = (d["name"], d["version"])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def parse_directory(root: Path) -> List[dict]:
    deps: List[dict] = []
    for path in root.rglob("go.mod"):
        try:
            deps.extend(parse_go_mod(path.read_text(errors="replace")))
        except OSError:
            pass
    return deps
