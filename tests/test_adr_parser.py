"""
Tests for adr_parser.py — Architecture Decision Record parsing.

Covers:
- ADR file discovery (docs/adr/*.md, decisions/*.md, etc.)
- MADR format parsing (title, status, context, decision, consequences)
- Nygard-style format parsing
- Status extraction (accepted, rejected, deprecated, proposed)
- Commit message decision signal extraction
- Files with no meaningful content skipped
- Empty directory produces no records
- parse_commit_decisions filtering (too short, no pattern)
- DecisionRecord to_event_payload() serialization
"""
import sys
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "git-analyzer-service"))

from parsers.adr_parser import (
    parse_adr_files,
    parse_commit_decisions,
    DecisionRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. ADR file discovery
# ---------------------------------------------------------------------------

class TestADRDiscovery:
    def test_docs_adr_directory_found(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "0001-use-postgres.md", """
            # Use PostgreSQL as primary database

            ## Status
            accepted

            ## Context
            We need a reliable relational database.

            ## Decision
            We will use PostgreSQL 15.
        """)
        records = parse_adr_files(str(tmp_path), "repo-1")
        assert len(records) == 1

    def test_decisions_directory_found(self, tmp_path):
        _write(tmp_path / "docs" / "decisions" / "adr-001.md", """
            # ADR 001: Use Kafka for messaging

            ## Status
            accepted

            ## Context
            Async messaging needed.

            ## Decision
            Use Apache Kafka.
        """)
        records = parse_adr_files(str(tmp_path), "repo-2")
        assert len(records) == 1

    def test_multiple_adr_files_all_parsed(self, tmp_path):
        for i in range(3):
            _write(tmp_path / "docs" / "adr" / f"000{i}-decision.md", f"""
                # Decision {i}

                ## Status
                accepted

                ## Context
                Context {i}.

                ## Decision
                Decision {i}.
            """)
        records = parse_adr_files(str(tmp_path), "repo-3")
        assert len(records) == 3

    def test_empty_directory_returns_no_records(self, tmp_path):
        records = parse_adr_files(str(tmp_path), "repo-4")
        assert records == []

    def test_non_adr_markdown_not_parsed(self, tmp_path):
        """Regular README.md should not be picked up as ADR."""
        _write(tmp_path / "README.md", "# My Project\nThis is not an ADR.")
        records = parse_adr_files(str(tmp_path), "repo-5")
        assert len(records) == 0


# ---------------------------------------------------------------------------
# 2. ADR content parsing
# ---------------------------------------------------------------------------

class TestADRContentParsing:
    def test_title_extracted(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "0001-kafka.md", """
            # ADR 001: Use Kafka for Messaging

            ## Status
            accepted

            ## Decision
            Use Kafka for all async messaging.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert len(records) == 1
        assert "Kafka" in records[0].title

    def test_status_accepted_extracted(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "001.md", """
            # Use Redis for caching

            ## Status
            accepted

            ## Decision
            Use Redis as the cache layer.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert records[0].status == "accepted"

    def test_status_rejected_extracted(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "002.md", """
            # Use MongoDB

            ## Status
            rejected

            ## Decision
            We considered MongoDB but rejected it.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert records[0].status == "rejected"

    def test_status_deprecated_extracted(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "003.md", """
            # Old Architecture

            ## Status
            deprecated

            ## Decision
            This approach is superseded.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert records[0].status == "deprecated"

    def test_context_section_extracted(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "001.md", """
            # ADR: PostgreSQL

            ## Status
            accepted

            ## Context
            We need a relational database for transactional data.

            ## Decision
            Use PostgreSQL.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert "relational database" in records[0].context

    def test_decision_section_extracted(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "001.md", """
            # ADR: Event Sourcing

            ## Status
            accepted

            ## Context
            Audit trail needed.

            ## Decision
            We will implement event sourcing using Kafka and the outbox pattern.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert "event sourcing" in records[0].decision.lower()

    def test_consequences_section_extracted(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "001.md", """
            # ADR: Microservices

            ## Status
            accepted

            ## Decision
            Split into microservices.

            ## Consequences
            Higher operational complexity but better scalability.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert "scalability" in records[0].consequences

    def test_repository_id_preserved(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "001.md", """
            # Decision

            ## Status
            proposed

            ## Decision
            Something.
        """)
        records = parse_adr_files(str(tmp_path), "my-repo-123")
        assert records[0].repository_id == "my-repo-123"

    def test_source_file_is_relative_path(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "001.md", """
            # Decision

            ## Status
            accepted

            ## Decision
            Do it.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert "docs/adr/001.md" in records[0].source_file.replace("\\", "/")

    def test_unknown_status_when_missing(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "001.md", """
            # Decision: Use Docker

            ## Context
            Container needed.

            ## Decision
            Use Docker for containerisation.
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert records[0].status == "unknown"

    def test_file_without_decision_section_skipped(self, tmp_path):
        _write(tmp_path / "docs" / "adr" / "empty.md", """
            # Empty ADR

            ## Status
            proposed
        """)
        records = parse_adr_files(str(tmp_path), "repo")
        assert len(records) == 0


# ---------------------------------------------------------------------------
# 3. Commit message decisions
# ---------------------------------------------------------------------------

class TestCommitDecisions:
    def _commit(self, sha: str, message: str) -> dict:
        return {
            "sha":         sha,
            "message":     message,
            "committedAt": "2024-01-15T10:00:00+00:00",
            "authorName":  "Dev",
        }

    def test_reason_prefix_detected(self):
        commits = [self._commit("s1", "refactor: extract service\nreason: reduce coupling in payment module")]
        records = parse_commit_decisions(commits, "repo-1")
        assert len(records) == 1
        assert "coupling" in records[0].decision.lower()

    def test_decision_prefix_detected(self):
        commits = [self._commit("s1", "feat: new auth\ndecision: use JWT tokens for stateless auth")]
        records = parse_commit_decisions(commits, "repo-1")
        assert len(records) == 1

    def test_adr_prefix_detected(self):
        commits = [self._commit("s1", "fix: update config\nadr: use environment variables for all secrets")]
        records = parse_commit_decisions(commits, "repo-1")
        assert len(records) == 1

    def test_decided_to_detected(self):
        commits = [self._commit("s1", "decided to use repository pattern for all database access")]
        records = parse_commit_decisions(commits, "repo-1")
        assert len(records) >= 1

    def test_short_signal_skipped(self):
        commits = [self._commit("s1", "reason: fix")]  # too short
        records = parse_commit_decisions(commits, "repo-1")
        assert len(records) == 0

    def test_normal_commit_produces_no_record(self):
        commits = [
            self._commit("s1", "fix: correct typo in readme"),
            self._commit("s2", "feat: add user profile page"),
        ]
        records = parse_commit_decisions(commits, "repo-1")
        assert len(records) == 0

    def test_source_type_is_commit_message(self):
        commits = [self._commit("s1", "reason: switch to hexagonal architecture for testability")]
        records = parse_commit_decisions(commits, "repo-1")
        assert records[0].source_type == "commit_message"

    def test_status_accepted_for_commit_decisions(self):
        commits = [self._commit("s1", "decision: use Redis for session storage in production")]
        records = parse_commit_decisions(commits, "repo-1")
        assert records[0].status == "accepted"

    def test_source_file_contains_sha(self):
        commits = [self._commit("abc1234", "reason: chose postgres for ACID compliance and joins")]
        records = parse_commit_decisions(commits, "repo-1")
        assert "abc1234" in records[0].source_file


# ---------------------------------------------------------------------------
# 4. Serialization
# ---------------------------------------------------------------------------

class TestDecisionSerialization:
    def _make_record(self) -> DecisionRecord:
        return DecisionRecord(
            title="Use PostgreSQL",
            status="accepted",
            context="Need ACID compliance.",
            decision="Use PostgreSQL 15 as primary database.",
            consequences="Operational overhead.",
            source_file="docs/adr/001.md",
            repository_id="repo-123",
            recorded_at="2024-01-15T10:00:00+00:00",
            source_type="adr_file",
        )

    def test_to_event_payload_has_required_keys(self):
        record = self._make_record()
        payload = record.to_event_payload()
        for key in ("title", "status", "context", "decision", "consequences",
                    "sourceFile", "repositoryId", "recordedAt", "sourceType"):
            assert key in payload

    def test_to_event_payload_truncates_long_context(self):
        record = self._make_record()
        record.context = "A" * 5000
        payload = record.to_event_payload()
        assert len(payload["context"]) <= 2000

    def test_repository_id_in_payload(self):
        record = self._make_record()
        payload = record.to_event_payload()
        assert payload["repositoryId"] == "repo-123"
