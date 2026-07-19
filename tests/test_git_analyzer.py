"""
tests/test_git_analyzer.py

Unit tests for git-analyzer-service dependency parsers and event payloads.

Run with:
  PYTHONPATH=src python -m pytest tests/test_git_analyzer.py -v
"""
import sys
import os

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "src", "git-analyzer-service"))

import pytest
from parsers import python_parser, node_parser, go_parser, rust_parser
from shared.models import (
    DependencyDetectedPayload,
    CommitAnalyzedPayload,
    RepositoryClonedPayload,
    RepositorySyncRequestedPayload,
    create_event,
)
from datetime import datetime, timezone


# =============================================================================
# Python parser
# =============================================================================

class TestPythonParserRequirementsTxt:

    def test_simple_pinned_dependency(self):
        content = "requests==2.31.0\n"
        deps = python_parser.parse_requirements_txt(content)
        assert len(deps) == 1
        assert deps[0]["name"] == "requests"
        assert deps[0]["version"] == "==2.31.0"
        assert deps[0]["ecosystem"] == "pip"

    def test_multiple_deps(self):
        content = "fastapi>=0.110.0\nuvicorn[standard]>=0.29.0\npydantic>=2.0\n"
        deps = python_parser.parse_requirements_txt(content)
        names = [d["name"] for d in deps]
        assert "fastapi" in names
        assert "uvicorn" in names
        assert "pydantic" in names

    def test_comments_ignored(self):
        content = "# This is a comment\nrequests==2.28.0\n"
        deps = python_parser.parse_requirements_txt(content)
        assert len(deps) == 1

    def test_blank_lines_ignored(self):
        content = "\nrequests==2.28.0\n\n"
        deps = python_parser.parse_requirements_txt(content)
        assert len(deps) == 1

    def test_dash_r_lines_ignored(self):
        content = "-r base.txt\nrequests==2.28.0\n"
        deps = python_parser.parse_requirements_txt(content)
        assert len(deps) == 1

    def test_git_url_lines_ignored(self):
        content = "git+https://github.com/user/repo.git\nrequests==2.28.0\n"
        deps = python_parser.parse_requirements_txt(content)
        assert len(deps) == 1

    def test_extras_stripped_from_name(self):
        content = "uvicorn[standard]>=0.29.0\n"
        deps = python_parser.parse_requirements_txt(content)
        assert deps[0]["name"] == "uvicorn"

    def test_dep_without_version(self):
        content = "requests\n"
        deps = python_parser.parse_requirements_txt(content)
        assert deps[0]["name"] == "requests"
        assert deps[0]["version"] == ""

    def test_ecosystem_is_pip(self):
        deps = python_parser.parse_requirements_txt("flask==3.0.0\n")
        assert deps[0]["ecosystem"] == "pip"

    def test_source_file_is_requirements_txt(self):
        deps = python_parser.parse_requirements_txt("flask==3.0.0\n")
        assert deps[0]["sourceFile"] == "requirements.txt"

    def test_empty_content_returns_empty_list(self):
        assert python_parser.parse_requirements_txt("") == []
        assert python_parser.parse_requirements_txt("# only comments\n") == []


class TestPythonParserSetupPy:

    def test_simple_install_requires(self):
        content = """
setup(
    install_requires=[
        'requests>=2.28.0',
        'flask==3.0.0',
    ]
)
"""
        deps = python_parser.parse_setup_py(content)
        names = [d["name"] for d in deps]
        assert "requests" in names
        assert "flask" in names

    def test_no_install_requires_returns_empty(self):
        assert python_parser.parse_setup_py("setup(name='x')") == []


# =============================================================================
# Node parser
# =============================================================================

class TestNodeParser:

    def test_dependencies(self):
        content = '{"dependencies": {"express": "^4.18.0", "lodash": "~4.17.21"}}'
        deps = node_parser.parse_package_json(content)
        names = [d["name"] for d in deps]
        assert "express" in names
        assert "lodash" in names

    def test_dev_dependencies(self):
        content = '{"devDependencies": {"jest": "^29.0.0"}}'
        deps = node_parser.parse_package_json(content)
        assert any(d["name"] == "jest" for d in deps)

    def test_peer_dependencies(self):
        content = '{"peerDependencies": {"react": ">=18.0.0"}}'
        deps = node_parser.parse_package_json(content)
        assert any(d["name"] == "react" for d in deps)

    def test_ecosystem_is_npm(self):
        content = '{"dependencies": {"express": "^4.0.0"}}'
        deps = node_parser.parse_package_json(content)
        assert deps[0]["ecosystem"] == "npm"

    def test_invalid_json_returns_empty(self):
        assert node_parser.parse_package_json("{not json}") == []

    def test_empty_deps_section_returns_empty(self):
        assert node_parser.parse_package_json("{}") == []

    def test_version_preserved(self):
        content = '{"dependencies": {"axios": "1.6.0"}}'
        deps = node_parser.parse_package_json(content)
        assert deps[0]["version"] == "1.6.0"


# =============================================================================
# Go parser
# =============================================================================

class TestGoParser:

    def test_single_require(self):
        content = "module example.com/myapp\n\nrequire github.com/gin-gonic/gin v1.9.1\n"
        deps = go_parser.parse_go_mod(content)
        assert any(d["name"] == "github.com/gin-gonic/gin" for d in deps)

    def test_block_require(self):
        content = """module example.com/app
go 1.21

require (
    github.com/gin-gonic/gin v1.9.1
    github.com/stretchr/testify v1.8.4
)
"""
        deps = go_parser.parse_go_mod(content)
        names = [d["name"] for d in deps]
        assert "github.com/gin-gonic/gin" in names
        assert "github.com/stretchr/testify" in names

    def test_ecosystem_is_go(self):
        content = "require github.com/pkg/errors v0.9.1\n"
        deps = go_parser.parse_go_mod(content)
        assert deps[0]["ecosystem"] == "go"

    def test_version_preserved(self):
        content = "require github.com/pkg/errors v0.9.1\n"
        deps = go_parser.parse_go_mod(content)
        assert deps[0]["version"] == "v0.9.1"

    def test_no_require_returns_empty(self):
        content = "module example.com/app\ngo 1.21\n"
        deps = go_parser.parse_go_mod(content)
        assert deps == []

    def test_deduplication(self):
        # Same dep listed twice shouldn't appear twice
        content = "require (\n    github.com/pkg/errors v0.9.1\n    github.com/pkg/errors v0.9.1\n)\n"
        deps = go_parser.parse_go_mod(content)
        names = [d["name"] for d in deps if d["name"] == "github.com/pkg/errors"]
        assert len(names) == 1


# =============================================================================
# Rust parser
# =============================================================================

class TestRustParser:

    CARGO_TOML = """
[package]
name = "myapp"
version = "0.1.0"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
tokio = "1.32.0"

[dev-dependencies]
mockito = "1.1.0"
"""

    def test_regular_dependencies(self):
        deps = rust_parser.parse_cargo_toml(self.CARGO_TOML)
        names = [d["name"] for d in deps]
        assert "serde" in names
        assert "tokio" in names

    def test_dev_dependencies(self):
        deps = rust_parser.parse_cargo_toml(self.CARGO_TOML)
        assert any(d["name"] == "mockito" for d in deps)

    def test_ecosystem_is_cargo(self):
        deps = rust_parser.parse_cargo_toml(self.CARGO_TOML)
        assert all(d["ecosystem"] == "cargo" for d in deps)

    def test_table_dep_version_extracted(self):
        deps = rust_parser.parse_cargo_toml(self.CARGO_TOML)
        serde = next(d for d in deps if d["name"] == "serde")
        assert serde["version"] == "1.0"

    def test_string_dep_version_preserved(self):
        deps = rust_parser.parse_cargo_toml(self.CARGO_TOML)
        tokio = next(d for d in deps if d["name"] == "tokio")
        assert tokio["version"] == "1.32.0"

    def test_invalid_toml_returns_empty(self):
        assert rust_parser.parse_cargo_toml("[not valid") == []


# =============================================================================
# Shared models — Phase 2 payload fields
# =============================================================================

class TestPhase2PayloadModels:

    def test_dependency_detected_payload_fields(self):
        p = DependencyDetectedPayload(
            repositoryId="repo_1",
            name="requests",
            version="==2.31.0",
            ecosystem="pip",
            sourceFile="requirements.txt",
        )
        assert p.repository_id == "repo_1"
        assert p.name == "requests"
        assert p.ecosystem == "pip"

    def test_dependency_detected_ecosystem_defaults(self):
        p = DependencyDetectedPayload(repositoryId="r1", name="pkg")
        assert p.ecosystem == "unknown"
        assert p.version == ""

    def test_commit_analyzed_payload_fields(self):
        now = datetime.now(timezone.utc)
        p = CommitAnalyzedPayload(
            repositoryId="repo_1",
            sha="abc123def456",
            authorEmail="dev@example.com",
            authorName="Alice",
            message="feat: add feature",
            filesChanged=["src/main.py", "tests/test_main.py"],
            committedAt=now,
        )
        assert p.sha == "abc123def456"
        assert p.author_email == "dev@example.com"
        assert len(p.files_changed) == 2

    def test_commit_analyzed_files_changed_defaults_empty(self):
        now = datetime.now(timezone.utc)
        p = CommitAnalyzedPayload(
            repositoryId="r1", sha="abc", authorEmail="x@y.com", committedAt=now
        )
        assert p.files_changed == []

    def test_repository_cloned_payload(self):
        p = RepositoryClonedPayload(
            repositoryId="repo_1",
            url="https://github.com/acme/app",
            defaultBranch="main",
            commitCount=42,
            sizeKb=1024,
        )
        assert p.commit_count == 42
        assert p.size_kb == 1024

    def test_repository_sync_requested_payload(self):
        p = RepositorySyncRequestedPayload(
            repositoryId="repo_1",
            url="https://github.com/acme/app",
            requestedBy="user_1",
        )
        assert p.url == "https://github.com/acme/app"
        assert p.default_branch == "main"

    def test_dependency_detected_event_serialization(self):
        p = DependencyDetectedPayload(
            repositoryId="r1", name="requests", version="2.31.0",
            ecosystem="pip", sourceFile="requirements.txt",
        )
        env = create_event("DependencyDetected", "r1", "org_1", p)
        data = env.model_dump(by_alias=True)
        assert data["eventType"] == "DependencyDetected"
        assert data["payload"]["name"] == "requests"
        assert data["payload"]["ecosystem"] == "pip"

    def test_commit_analyzed_event_serialization(self):
        now = datetime.now(timezone.utc)
        p = CommitAnalyzedPayload(
            repositoryId="r1", sha="abc123", authorEmail="dev@x.com",
            message="fix: bug", filesChanged=["main.py"], committedAt=now,
        )
        env = create_event("CommitAnalyzed", "abc123", "org_1", p)
        data = env.model_dump(by_alias=True)
        assert data["payload"]["sha"] == "abc123"
        assert data["payload"]["authorEmail"] == "dev@x.com"
        assert data["payload"]["filesChanged"] == ["main.py"]
