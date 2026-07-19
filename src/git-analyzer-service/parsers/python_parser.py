"""
parsers/python_parser.py

Detects Python dependencies from:
  - requirements.txt  (one dep per line, supports ==, >=, <=, ~=, !=, extras)
  - pyproject.toml    ([project] dependencies / [tool.poetry.dependencies])
  - setup.py          (install_requires list, best-effort regex)
"""
import re
from pathlib import Path
from typing import List, Tuple


# Normalise a PEP-508 specifier like "requests>=2.28.0,<3" → ("requests", ">=2.28.0,<3")
_VERSION_OPS = re.compile(r"[><=!~^]")


def _split_name_version(spec: str) -> Tuple[str, str]:
    """Split 'package>=1.0' into ('package', '>=1.0')."""
    spec = spec.strip()
    # Strip extras: requests[security]>=2 → requests>=2
    spec = re.sub(r"\[.*?\]", "", spec)
    match = _VERSION_OPS.search(spec)
    if match:
        return spec[: match.start()].strip(), spec[match.start() :].strip()
    return spec.strip(), ""


def parse_requirements_txt(content: str) -> List[dict]:
    """Parse requirements.txt content into a list of {name, version} dicts."""
    deps = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Skip editable / URL installs
        if line.startswith("git+") or line.startswith("http"):
            continue
        name, version = _split_name_version(line)
        if name:
            deps.append({"name": name, "version": version, "ecosystem": "pip",
                         "sourceFile": "requirements.txt"})
    return deps


def parse_pyproject_toml(content: str) -> List[dict]:
    """Parse pyproject.toml (PEP 621 [project] or Poetry) dependency lists."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # fallback
        except ImportError:
            try:
                import toml as tomllib  # third-party
            except ImportError:
                return []   # graceful: toml parser unavailable

    try:
        data = tomllib.loads(content)
    except Exception:
        return []

    deps = []

    # PEP 621 style
    for spec in data.get("project", {}).get("dependencies", []):
        name, version = _split_name_version(spec)
        if name:
            deps.append({"name": name, "version": version, "ecosystem": "pip",
                         "sourceFile": "pyproject.toml"})

    # Poetry style
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for pkg, ver in poetry_deps.items():
        if pkg.lower() == "python":
            continue
        version = ver if isinstance(ver, str) else ""
        deps.append({"name": pkg, "version": version, "ecosystem": "pip",
                     "sourceFile": "pyproject.toml"})

    return deps


def parse_setup_py(content: str) -> List[dict]:
    """Best-effort regex extraction from setup.py install_requires."""
    pattern = re.compile(
        r"install_requires\s*=\s*\[([^\]]+)\]", re.DOTALL
    )
    match = pattern.search(content)
    if not match:
        return []
    raw = match.group(1)
    deps = []
    for item in re.findall(r"['\"]([^'\"]+)['\"]", raw):
        name, version = _split_name_version(item)
        if name:
            deps.append({"name": name, "version": version, "ecosystem": "pip",
                         "sourceFile": "setup.py"})
    return deps


def parse_directory(root: Path) -> List[dict]:
    """Scan a repo directory and parse all Python dependency files."""
    deps: List[dict] = []
    for path in root.rglob("requirements*.txt"):
        try:
            deps.extend(parse_requirements_txt(path.read_text(errors="replace")))
        except OSError:
            pass
    for path in root.rglob("pyproject.toml"):
        try:
            deps.extend(parse_pyproject_toml(path.read_text(errors="replace")))
        except OSError:
            pass
    for path in root.rglob("setup.py"):
        try:
            deps.extend(parse_setup_py(path.read_text(errors="replace")))
        except OSError:
            pass
    return deps
