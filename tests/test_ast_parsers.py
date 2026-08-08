"""
tests/test_ast_parsers.py

Unit tests for the AST-based static analysis parsers:
  - parsers/ast_python.py  (Python ast module)
  - parsers/ast_javascript.py (regex-based JS/TS)

These tests use in-memory strings / tmp files — no real git clone needed.
"""
import sys, os, tempfile
from pathlib import Path
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "git-analyzer-service"))

from parsers import ast_python, ast_javascript


# =============================================================================
# Helpers
# =============================================================================

def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# =============================================================================
# Python AST parser
# =============================================================================

class TestAstPythonParseFile:

    def _parse(self, code: str, filename="module.py"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write(root, filename, code)
            return ast_python.parse_file(path, root)

    # ── Symbol extraction ──────────────────────────────────────────────────

    def test_extracts_top_level_function(self):
        syms, _ = self._parse("def foo():\n    pass\n")
        names = [s["name"] for s in syms]
        assert "foo" in names

    def test_extracts_async_function(self):
        syms, _ = self._parse("async def bar():\n    pass\n")
        names = [s["name"] for s in syms]
        assert "bar" in names

    def test_extracts_class(self):
        syms, _ = self._parse("class MyClass:\n    pass\n")
        names = [s["name"] for s in syms]
        assert "MyClass" in names

    def test_extracts_method_with_qualified_name(self):
        code = "class Svc:\n    def process(self):\n        pass\n"
        syms, _ = self._parse(code)
        names = [s["name"] for s in syms]
        assert "Svc.process" in names

    def test_symbol_type_function(self):
        syms, _ = self._parse("def foo():\n    pass\n")
        fn = next(s for s in syms if s["name"] == "foo")
        assert fn["symbolType"] == "function"

    def test_symbol_type_class(self):
        syms, _ = self._parse("class MyClass:\n    pass\n")
        cls = next(s for s in syms if s["name"] == "MyClass")
        assert cls["symbolType"] == "class"

    def test_line_number_recorded(self):
        syms, _ = self._parse("\n\ndef foo():\n    pass\n")
        fn = next(s for s in syms if s["name"] == "foo")
        assert fn["lineNumber"] == 3

    def test_language_is_python(self):
        syms, _ = self._parse("def foo():\n    pass\n")
        assert all(s["language"] == "python" for s in syms)

    def test_empty_file_returns_empty(self):
        syms, rels = self._parse("")
        assert syms == []
        assert rels == []

    def test_syntax_error_returns_empty(self):
        syms, rels = self._parse("def broken(\n    pass")
        assert syms == []

    # ── CALLS detection ───────────────────────────────────────────────────

    def test_direct_call_detected(self):
        code = "def foo():\n    bar()\n\ndef bar():\n    pass\n"
        _, rels = self._parse(code)
        calls = [r for r in rels if r["relationType"] == "CALLS"]
        to_names = [r["toSymbol"] for r in calls]
        assert "bar" in to_names

    def test_self_call_excluded(self):
        # foo calling foo (recursion) — should not appear as a CALLS relation
        code = "def foo():\n    foo()\n"
        _, rels = self._parse(code)
        calls = [r for r in rels if r["relationType"] == "CALLS"]
        assert not any(r["fromSymbol"] == "foo" and r["toSymbol"] == "foo" for r in calls)

    def test_method_call_on_self_detected(self):
        code = "class Svc:\n    def run(self):\n        self.helper()\n    def helper(self):\n        pass\n"
        _, rels = self._parse(code)
        calls = [r for r in rels if r["relationType"] == "CALLS"]
        assert any(r["toSymbol"] == "helper" for r in calls)

    # ── IMPLEMENTS detection ──────────────────────────────────────────────

    def test_inheritance_detected(self):
        code = "class Child(Parent):\n    pass\n"
        _, rels = self._parse(code)
        impls = [r for r in rels if r["relationType"] == "IMPLEMENTS"]
        assert any(r["fromSymbol"] == "Child" and r["toSymbol"] == "Parent"
                   for r in impls)

    def test_object_base_excluded(self):
        code = "class Foo(object):\n    pass\n"
        _, rels = self._parse(code)
        impls = [r for r in rels if r["relationType"] == "IMPLEMENTS"]
        assert not any(r["toSymbol"] == "object" for r in impls)

    def test_multiple_bases_detected(self):
        code = "class Foo(Bar, Baz):\n    pass\n"
        _, rels = self._parse(code)
        impls = [r for r in rels if r["relationType"] == "IMPLEMENTS"]
        targets = {r["toSymbol"] for r in impls}
        assert "Bar" in targets
        assert "Baz" in targets

    def test_file_path_relative(self):
        syms, _ = self._parse("def foo():\n    pass\n", "sub/module.py")
        assert all("sub/module.py" in s["filePath"] for s in syms)


class TestAstPythonParseDirectory:

    def test_scans_multiple_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "a.py", "def foo():\n    pass\n")
            _write(root, "b.py", "def bar():\n    pass\n")
            syms, _ = ast_python.parse_directory(root)
        names = [s["name"] for s in syms]
        assert "foo" in names
        assert "bar" in names

    def test_skips_test_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "service.py", "def real():\n    pass\n")
            _write(root, "test_service.py", "def test_real():\n    pass\n")
            syms, _ = ast_python.parse_directory(root)
        names = [s["name"] for s in syms]
        assert "real" in names
        assert "test_real" not in names

    def test_skips_venv_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/app.py", "def app():\n    pass\n")
            _write(root, ".venv/lib/site-packages/pkg.py", "def venv_fn():\n    pass\n")
            syms, _ = ast_python.parse_directory(root)
        names = [s["name"] for s in syms]
        assert "app" in names
        assert "venv_fn" not in names

    def test_returns_empty_for_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            syms, rels = ast_python.parse_directory(root)
        assert syms == []
        assert rels == []


# =============================================================================
# JavaScript / TypeScript regex parser
# =============================================================================

class TestAstJavaScriptParseFile:

    def _parse(self, code: str, filename="module.js"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write(root, filename, code)
            return ast_javascript.parse_file(path, root)

    # ── Symbol extraction ──────────────────────────────────────────────────

    def test_named_function_detected(self):
        syms, _ = self._parse("function greet(name) {\n  return name;\n}\n")
        names = [s["name"] for s in syms]
        assert "greet" in names

    def test_arrow_function_detected(self):
        syms, _ = self._parse("const compute = (x) => x * 2;\n")
        names = [s["name"] for s in syms]
        assert "compute" in names

    def test_class_detected(self):
        syms, _ = self._parse("class PaymentService {\n  process() {}\n}\n")
        names = [s["name"] for s in syms]
        assert "PaymentService" in names

    def test_symbol_type_function(self):
        syms, _ = self._parse("function foo() {}\n")
        fn = next(s for s in syms if s["name"] == "foo")
        assert fn["symbolType"] == "function"

    def test_symbol_type_class(self):
        syms, _ = self._parse("class Bar {}\n")
        cls = next(s for s in syms if s["name"] == "Bar")
        assert cls["symbolType"] == "class"

    def test_language_is_javascript(self):
        syms, _ = self._parse("function foo() {}\n")
        assert all(s["language"] == "javascript" for s in syms)

    def test_empty_file_returns_empty(self):
        syms, rels = self._parse("")
        assert syms == []
        assert rels == []

    def test_typescript_file_parsed(self):
        code = "class UserService {\n  getUser(id: string): User { return null; }\n}\n"
        syms, _ = self._parse(code, "service.ts")
        names = [s["name"] for s in syms]
        assert "UserService" in names

    # ── IMPLEMENTS (extends) ──────────────────────────────────────────────

    def test_extends_detected(self):
        code = "class Dog extends Animal {\n  bark() {}\n}\n"
        _, rels = self._parse(code)
        impls = [r for r in rels if r["relationType"] == "IMPLEMENTS"]
        assert any(r["fromSymbol"] == "Dog" and r["toSymbol"] == "Animal"
                   for r in impls)

    def test_no_extends_no_implements_relation(self):
        code = "class Standalone {\n  run() {}\n}\n"
        _, rels = self._parse(code)
        impls = [r for r in rels if r["relationType"] == "IMPLEMENTS"]
        assert impls == []

    # ── CALLS ─────────────────────────────────────────────────────────────

    def test_call_expression_detected(self):
        code = "function main() {\n  helper();\n}\nfunction helper() {}\n"
        _, rels = self._parse(code)
        calls = [r for r in rels if r["relationType"] == "CALLS"]
        assert any(r["toSymbol"] == "helper" for r in calls)

    def test_builtin_calls_excluded(self):
        code = "function foo() {\n  console.log('hi');\n  JSON.parse('{}');\n}\n"
        _, rels = self._parse(code)
        calls = [r for r in rels if r["relationType"] == "CALLS"]
        to_names = [r["toSymbol"] for r in calls]
        assert "console" not in to_names
        assert "JSON" not in to_names


class TestAstJavaScriptParseDirectory:

    def test_scans_js_and_ts_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "a.js",  "function jsFunc() {}\n")
            _write(root, "b.ts",  "class TsClass {}\n")
            _write(root, "c.jsx", "const JsxComp = () => null;\n")
            syms, _ = ast_javascript.parse_directory(root)
        names = [s["name"] for s in syms]
        assert "jsFunc" in names
        assert "TsClass" in names
        assert "JsxComp" in names

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/app.js", "function myApp() {}\n")
            _write(root, "node_modules/pkg/index.js", "function pkgFn() {}\n")
            syms, _ = ast_javascript.parse_directory(root)
        names = [s["name"] for s in syms]
        assert "myApp" in names
        assert "pkgFn" not in names

    def test_skips_minified_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "app.js", "function real() {}\n")
            _write(root, "app.min.js", "function minified() {}\n")
            syms, _ = ast_javascript.parse_directory(root)
        names = [s["name"] for s in syms]
        assert "real" in names
        assert "minified" not in names

    def test_non_js_files_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "README.md", "# hello\n")
            _write(root, "config.yaml", "key: value\n")
            syms, _ = ast_javascript.parse_directory(root)
        assert syms == []
