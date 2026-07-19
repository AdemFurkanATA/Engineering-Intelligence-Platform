"""
parsers/node_parser.py

Detects Node.js dependencies from package.json files.
Parses both `dependencies` and `devDependencies`.
"""
import json
from pathlib import Path
from typing import List


def parse_package_json(content: str, source_file: str = "package.json") -> List[dict]:
    """Parse package.json content."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []

    deps = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, version in data.get(section, {}).items():
            deps.append({
                "name":       name,
                "version":    version if isinstance(version, str) else "",
                "ecosystem":  "npm",
                "sourceFile": source_file,
            })
    return deps


def parse_directory(root: Path) -> List[dict]:
    """Scan for all package.json files (excluding node_modules)."""
    deps: List[dict] = []
    for path in root.rglob("package.json"):
        # Skip node_modules
        if "node_modules" in path.parts:
            continue
        try:
            deps.extend(parse_package_json(
                path.read_text(errors="replace"),
                source_file=str(path.relative_to(root)),
            ))
        except OSError:
            pass
    return deps
