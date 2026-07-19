"""
parsers/rust_parser.py

Detects Rust dependencies from Cargo.toml files.
Parses [dependencies], [dev-dependencies], and [build-dependencies].
"""
from pathlib import Path
from typing import List


def parse_cargo_toml(content: str) -> List[dict]:
    """Parse Cargo.toml dependency sections."""
    try:
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                import toml as tomllib
        data = tomllib.loads(content)
    except Exception:
        return []

    deps = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in data.get(section, {}).items():
            if isinstance(spec, str):
                version = spec
            elif isinstance(spec, dict):
                version = spec.get("version", "")
            else:
                version = ""
            deps.append({"name": name, "version": version,
                         "ecosystem": "cargo", "sourceFile": "Cargo.toml"})
    return deps


def parse_directory(root: Path) -> List[dict]:
    deps: List[dict] = []
    for path in root.rglob("Cargo.toml"):
        try:
            deps.extend(parse_cargo_toml(path.read_text(errors="replace")))
        except OSError:
            pass
    return deps
