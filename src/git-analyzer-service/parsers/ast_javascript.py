"""
parsers/ast_javascript.py

Regex-based static analysis for JavaScript / TypeScript source files.

Extracts:
  - CodeSymbol: named functions, arrow functions, classes
  - CodeRelation:
      CALLS     — function call expressions (best-effort)
      IMPLEMENTS — class inheritance via "extends"

Design decision (Phase 2):
  tree-sitter would provide proper AST but introduces a Docker dependency.
  Regex is sufficient for the common patterns and is zero-dependency.
  Phase 3 can upgrade to tree-sitter for full accuracy.

Handled patterns:
  function foo(...)          — named function declaration
  const foo = (...) => ...   — arrow function
  const foo = function(...)  — function expression
  class Foo { ... }          — class declaration
  class Foo extends Bar { }  — inheritance
  foo(...)                   — call expression
  this.foo(...)              — method call (→ "foo")
  Obj.method(...)            — member call (→ "Obj.method")
"""
import re
from pathlib import Path
from typing import List, Tuple

_MAX_SYMBOLS_PER_FILE = 200
_MAX_CALLS_PER_FILE = 300

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Named function: function foo(...) or async function foo(...)
_RE_FUNC_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
    re.MULTILINE,
)

# Arrow / expression: const foo = (...) => or const foo = function(
_RE_ARROW = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)\s*=>|function\s*\()",
    re.MULTILINE,
)

# Class: class Foo [extends Bar] {
_RE_CLASS = re.compile(
    r"class\s+([A-Za-z_$][A-Za-z0-9_$]*)(?:\s+extends\s+([A-Za-z_$][A-Za-z0-9_$.]*))?\s*\{",
    re.MULTILINE,
)

# Call expression (rough): identifier( or identifier.method(
# Negative look-behind for "function " and "class " to avoid false positives
_RE_CALL = re.compile(
    r"(?<!\bfunction\s)(?<!\bclass\s)\b([A-Za-z_$][A-Za-z0-9_$]*)(?:\.([A-Za-z_$][A-Za-z0-9_$]*))?\s*\(",
    re.MULTILINE,
)

# Symbols to ignore as call targets (JS builtins / common patterns)
_CALL_IGNORE = frozenset({
    "if", "for", "while", "switch", "catch", "function", "class",
    "return", "typeof", "instanceof", "new", "delete", "void",
    "console", "require", "import", "exports", "module",
    "Promise", "Array", "Object", "String", "Number", "Boolean",
    "parseInt", "parseFloat", "setTimeout", "setInterval", "clearTimeout",
    "JSON", "Math", "Date", "Error", "Map", "Set", "Symbol",
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _lineno(source: str, match_start: int) -> int:
    """Return 1-based line number for a character offset."""
    return source[:match_start].count("\n") + 1


# ---------------------------------------------------------------------------
# Per-file analysis
# ---------------------------------------------------------------------------

def parse_file(path: Path, root: Path) -> Tuple[List[dict], List[dict]]:
    """Parse a single JS/TS file. Returns (symbols, relations)."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []

    rel_path = _rel(root, path)
    symbols: List[dict] = []
    relations: List[dict] = []
    known_symbols: set[str] = set()

    def _add_symbol(name: str, stype: str, lineno: int):
        if len(symbols) >= _MAX_SYMBOLS_PER_FILE:
            return
        symbols.append({
            "name": name,
            "symbolType": stype,
            "filePath": rel_path,
            "lineNumber": lineno,
            "language": "javascript",
        })
        known_symbols.add(name)

    # ── Classes ──────────────────────────────────────────────────────────────
    for m in _RE_CLASS.finditer(source):
        class_name = m.group(1)
        base_name  = m.group(2)
        _add_symbol(class_name, "class", _lineno(source, m.start()))
        if base_name:
            relations.append({
                "fromSymbol": class_name,
                "toSymbol": base_name,
                "relationType": "IMPLEMENTS",
                "filePath": rel_path,
            })

    # ── Named functions ───────────────────────────────────────────────────────
    for m in _RE_FUNC_DECL.finditer(source):
        _add_symbol(m.group(1), "function", _lineno(source, m.start()))

    # ── Arrow / expression functions ─────────────────────────────────────────
    for m in _RE_ARROW.finditer(source):
        name = m.group(1)
        if name not in known_symbols:  # avoid duplicating named functions
            _add_symbol(name, "function", _lineno(source, m.start()))

    # ── Call expressions ──────────────────────────────────────────────────────
    calls_found = 0
    for m in _RE_CALL.finditer(source):
        if calls_found >= _MAX_CALLS_PER_FILE:
            break
        obj    = m.group(1)
        method = m.group(2)

        if obj in _CALL_IGNORE:
            continue

        if method:
            callee = f"{obj}.{method}" if obj not in ("this", "self") else method
        else:
            callee = obj

        if callee and callee not in _CALL_IGNORE:
            # Attribute the call to the first symbol in the file (rough)
            from_sym = symbols[0]["name"] if symbols else "module"
            relations.append({
                "fromSymbol": from_sym,
                "toSymbol": callee,
                "relationType": "CALLS",
                "filePath": rel_path,
            })
            calls_found += 1

    return symbols, relations


# ---------------------------------------------------------------------------
# Directory scan
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "dist", "build",
    "coverage", ".next", "__pycache__",
})

_JS_EXTENSIONS = frozenset({".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"})

_SKIP_FILE_PATTERNS = (
    ".min.js", ".bundle.js", ".test.js", ".spec.js",
    ".test.ts", ".spec.ts",
)


def parse_directory(root: Path) -> Tuple[List[dict], List[dict]]:
    """Recursively analyse all JS/TS files under root."""
    all_symbols: List[dict] = []
    all_relations: List[dict] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _JS_EXTENSIONS:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if any(path.name.endswith(pat) for pat in _SKIP_FILE_PATTERNS):
            continue

        syms, rels = parse_file(path, root)
        all_symbols.extend(syms)
        all_relations.extend(rels)

    return all_symbols, all_relations
