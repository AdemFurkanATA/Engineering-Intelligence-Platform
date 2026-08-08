"""
parsers/ast_python.py

AST-based static analysis for Python source files.

Extracts:
  - CodeSymbol: every module-level / class-level function and class
  - CodeRelation:
      CALLS     — function/method call targets (best-effort, name-based)
      IMPLEMENTS — class inheritance (base classes)

Symbol ID format: "{relative_file_path}::{qualified_name}"
  e.g. "src/service.py::PaymentService.process"

Limitations (acceptable for Phase 2):
  - Dynamic calls (getattr, metaclass magic) are skipped
  - No cross-file name resolution — callee name is used as-is
  - Star imports and aliased imports are not resolved
"""
import ast
import os
from pathlib import Path
from typing import List, Tuple

# Max symbols per file to keep events bounded
_MAX_SYMBOLS_PER_FILE = 200
_MAX_CALLS_PER_FUNCTION = 50


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rel(root: Path, path: Path) -> str:
    """Return path relative to root, using forward slashes."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _qualified(class_name: str | None, symbol_name: str) -> str:
    """Build a qualified symbol name: ClassName.method or just name."""
    if class_name:
        return f"{class_name}.{symbol_name}"
    return symbol_name


def _extract_call_name(node: ast.expr) -> str | None:
    """
    Extract a best-effort string name from a Call.func node.

    Handles:
      foo()          → "foo"
      self.foo()     → "foo"
      obj.method()   → "method"
      Cls.method()   → "Cls.method"
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            # self.foo → "foo"; Cls.bar → "Cls.bar"
            obj = node.value.id
            if obj == "self" or obj == "cls":
                return node.attr
            return f"{obj}.{node.attr}"
        # deeper chains: just use the attribute name
        return node.attr
    return None


# ---------------------------------------------------------------------------
# Per-file analysis
# ---------------------------------------------------------------------------

def parse_file(
    path: Path, root: Path
) -> Tuple[List[dict], List[dict]]:
    """
    Parse a single .py file.

    Returns (symbols, relations) where each item is a dict compatible
    with CodeSymbol / CodeRelation aliases.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return [], []
    except OSError:
        return [], []

    rel_path = _rel(root, path)
    symbols: List[dict] = []
    relations: List[dict] = []

    def _visit_body(body, class_name: str | None = None):
        for node in body:
            if isinstance(node, ast.ClassDef):
                symbols.append({
                    "name": node.name,
                    "symbolType": "class",
                    "filePath": rel_path,
                    "lineNumber": node.lineno,
                    "language": "python",
                })
                # IMPLEMENTS: base classes
                for base in node.bases:
                    base_name = _extract_call_name(base) or (
                        base.id if isinstance(base, ast.Name) else None
                    )
                    if base_name and base_name not in ("object",):
                        relations.append({
                            "fromSymbol": node.name,
                            "toSymbol": base_name,
                            "relationType": "IMPLEMENTS",
                            "filePath": rel_path,
                        })
                # Recurse into class body
                _visit_body(node.body, class_name=node.name)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = _qualified(class_name, node.name)
                symbols.append({
                    "name": qname,
                    "symbolType": "function",
                    "filePath": rel_path,
                    "lineNumber": node.lineno,
                    "language": "python",
                })
                # CALLS: walk function body for Call nodes
                calls_found = 0
                for child in ast.walk(node):
                    if calls_found >= _MAX_CALLS_PER_FUNCTION:
                        break
                    if isinstance(child, ast.Call):
                        callee = _extract_call_name(child.func)
                        if callee and callee != qname:  # skip self-recursion
                            relations.append({
                                "fromSymbol": qname,
                                "toSymbol": callee,
                                "relationType": "CALLS",
                                "filePath": rel_path,
                            })
                            calls_found += 1

    _visit_body(tree.body)
    return symbols[:_MAX_SYMBOLS_PER_FILE], relations


# ---------------------------------------------------------------------------
# Directory scan
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "eggs",
    ".eggs", "site-packages",
})

_SKIP_FILES = frozenset({
    "setup.py",  # already parsed by python_parser for deps
    "conftest.py",
    "manage.py",
})


def parse_directory(root: Path) -> Tuple[List[dict], List[dict]]:
    """
    Recursively analyse all .py files under root.

    Returns aggregated (symbols, relations).
    """
    all_symbols: List[dict] = []
    all_relations: List[dict] = []
    files_visited = 0

    for path in root.rglob("*.py"):
        # Skip unwanted directories
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name in _SKIP_FILES:
            continue
        # Skip test files (large, generates noisy edges)
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            continue

        syms, rels = parse_file(path, root)
        all_symbols.extend(syms)
        all_relations.extend(rels)
        files_visited += 1

    return all_symbols, all_relations
