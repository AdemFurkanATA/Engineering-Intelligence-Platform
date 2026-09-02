"""
Tests for timeline_engine.py — entity timeline computation.

Covers:
- Basic timeline construction from commits
- birthDate / lastModified correctness
- changeFrequency calculation
- Churn accumulation
- Refactoring signal detection
- Status assignment (active / stale / deprecated)
- Repository-level timeline summary
- Entity path resolution from node properties
- Empty commit list handling
"""
import sys
import os
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "graph-service"))

from analyzers.timeline_engine import build_timeline, build_repo_timeline, EntityTimeline


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: int, hour: int = 0) -> str:
    dt = _NOW - timedelta(days=days_ago)
    return dt.replace(hour=hour).isoformat()


def _commit(sha: str, days_ago: int, message: str = "fix bug",
            files: list = None, added: int = 10, deleted: int = 5) -> dict:
    return {
        "sha":          sha,
        "message":      message,
        "authorName":   "Dev User",
        "authorEmail":  "dev@example.com",
        "committedAt":  _ts(days_ago),
        "filesChanged": files if files is not None else ["src/x.py"],
        "linesAdded":   added,
        "linesDeleted": deleted,
    }


def _node(label: str, name: str, path: str = None) -> dict:
    props = {"name": name}
    if path:
        props["filePath"] = path
    return {"label": label, "properties": props}


def _graph(nodes: dict, commits: list, rels: list = None) -> dict:
    return {"nodes": nodes, "relationships": rels or [], "commits": commits}


# ---------------------------------------------------------------------------
# 1. Basic timeline construction
# ---------------------------------------------------------------------------

class TestBasicTimeline:
    def test_birth_date_is_oldest_commit(self):
        nodes   = {"svc": _node("Service", "UserService", "src/service.py")}
        commits = [
            _commit("sha-new", 5,  files=["src/service.py"]),
            _commit("sha-old", 100, files=["src/service.py"]),
        ]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert result.birth_date == _ts(100)

    def test_last_modified_is_newest_commit(self):
        nodes   = {"svc": _node("Service", "UserService", "src/service.py")}
        commits = [
            _commit("sha-new", 3,  files=["src/service.py"]),
            _commit("sha-old", 90, files=["src/service.py"]),
        ]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert result.last_modified == _ts(3)

    def test_commit_count_correct(self):
        nodes   = {"svc": _node("Service", "UserService", "src/x.py")}
        commits = [_commit(f"sha-{i}", i + 1) for i in range(7)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert result.commit_count == 7

    def test_entity_name_from_node(self):
        nodes  = {"svc": _node("Service", "OrderService")}
        result = build_timeline("svc", _graph(nodes, []), now=_NOW)
        assert result.entity_name == "OrderService"

    def test_entity_type_from_node_label(self):
        nodes  = {"cls": {"label": "Class", "properties": {"name": "MyClass"}}}
        result = build_timeline("cls", _graph(nodes, []), now=_NOW)
        assert result.entity_type == "Class"

    def test_unknown_entity_has_no_dates(self):
        result = build_timeline("nonexistent", _graph({}, []), now=_NOW)
        assert result.birth_date is None
        assert result.last_modified is None


# ---------------------------------------------------------------------------
# 2. Churn calculation
# ---------------------------------------------------------------------------

class TestChurn:
    def test_churn_sums_added_and_deleted(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [
            _commit("s1", 1, added=20, deleted=5),
            _commit("s2", 2, added=10, deleted=3),
        ]
        result = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert result.total_churn == (20 + 5) + (10 + 3)

    def test_zero_churn_for_no_commits(self):
        result = build_timeline("svc", _graph({}, []), now=_NOW)
        assert result.total_churn == 0


# ---------------------------------------------------------------------------
# 3. Change frequency
# ---------------------------------------------------------------------------

class TestChangeFrequency:
    def test_frequency_zero_with_no_recent_commits(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        # All commits older than 30 days
        commits = [_commit("s1", 60), _commit("s2", 90)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW, churn_window=30)
        assert result.change_frequency == 0.0

    def test_frequency_reflects_recent_commits(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        # 8 commits in last 30 days → ~2 per week
        commits = [_commit(f"s{i}", i + 1) for i in range(8)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW, churn_window=28)
        # 8 commits / 4 weeks = 2.0 per week
        assert result.change_frequency == pytest.approx(2.0, abs=0.5)

    def test_frequency_rounded_in_to_dict(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 1)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        d = result.to_dict()
        assert isinstance(d["changeFrequency"], float)


# ---------------------------------------------------------------------------
# 4. Status assignment
# ---------------------------------------------------------------------------

class TestStatusAssignment:
    def test_active_when_recent_commit(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 5)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW,
                                  active_days=30)
        assert result.status == "active"

    def test_stale_after_active_window(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 90)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW,
                                  active_days=30, stale_days=180)
        assert result.status == "stale"

    def test_deprecated_after_stale_window(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 365)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW,
                                  active_days=30, stale_days=180)
        assert result.status == "deprecated"

    def test_unknown_status_when_no_commits(self):
        nodes  = {"svc": _node("Service", "X")}
        result = build_timeline("svc", _graph(nodes, []), now=_NOW)
        assert result.status == "unknown"


# ---------------------------------------------------------------------------
# 5. Refactoring signals
# ---------------------------------------------------------------------------

class TestRefactoringSignals:
    def test_refactor_keyword_detected(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [
            _commit("s1", 1, message="refactor: extract payment module"),
            _commit("s2", 2, message="fix: typo"),
        ]
        result = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert len(result.refactoring_signals) == 1
        assert result.refactoring_signals[0].sha == "s1"

    def test_rename_keyword_detected(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 1, message="rename UserService to AccountService")]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert any(e.sha == "s1" for e in result.refactoring_signals)

    def test_normal_commits_not_flagged_as_refactoring(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [
            _commit("s1", 1, message="feat: add user login"),
            _commit("s2", 2, message="fix: null pointer"),
        ]
        result = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert len(result.refactoring_signals) == 0

    def test_refactoring_event_type(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 1, message="restructure: split into modules")]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        assert result.refactoring_signals[0].event_type == "refactoring"


# ---------------------------------------------------------------------------
# 6. Recent events
# ---------------------------------------------------------------------------

class TestRecentEvents:
    def test_first_touch_event_type_assigned(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("sha-new", 1), _commit("sha-old", 100)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        event_types = {e.event_type for e in result.recent_events}
        assert "first_touch" in event_types
        assert "last_touch" in event_types

    def test_events_capped_at_max(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit(f"s{i}", i + 1) for i in range(30)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW, max_events=10)
        assert len(result.recent_events) <= 10


# ---------------------------------------------------------------------------
# 7. Serialization
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_has_required_keys(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 5)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        d = result.to_dict()
        for key in ("entityId", "entityName", "entityType", "birthDate",
                    "lastModified", "changeFrequency", "totalChurn",
                    "commitCount", "status", "refactoringSignals", "recentEvents"):
            assert key in d, f"Missing key: {key}"

    def test_event_to_dict_has_required_keys(self):
        nodes   = {"svc": _node("Service", "X", "src/x.py")}
        commits = [_commit("s1", 5)]
        result  = build_timeline("svc", _graph(nodes, commits), now=_NOW)
        if result.recent_events:
            ev = result.recent_events[0].to_dict()
            for key in ("sha", "message", "author", "committedAt", "eventType"):
                assert key in ev


# ---------------------------------------------------------------------------
# 8. Repository-level timeline
# ---------------------------------------------------------------------------

class TestRepoTimeline:
    def test_total_commits_count(self):
        commits = [_commit(f"s{i}", i) for i in range(10)]
        summary = build_repo_timeline({"commits": commits, "nodes": {}, "relationships": []},
                                       now=_NOW)
        assert summary["totalCommits"] == 10

    def test_active_contributors_listed(self):
        commits = []
        for i in range(5):
            c = _commit(f"s{i}", i)
            c["authorName"] = "Alice"
            commits.append(c)
        for i in range(3):
            c = _commit(f"t{i}", i + 5)
            c["authorName"] = "Bob"
            commits.append(c)
        summary = build_repo_timeline({"commits": commits, "nodes": {}, "relationships": []},
                                       now=_NOW)
        names = [a["name"] for a in summary["activeContributors"]]
        assert "Alice" in names
        assert "Bob" in names

    def test_empty_commits_returns_zero(self):
        summary = build_repo_timeline({"commits": [], "nodes": {}, "relationships": []})
        assert summary["totalCommits"] == 0

    def test_commits_by_week_has_week_and_count(self):
        commits = [_commit(f"s{i}", i * 2) for i in range(6)]
        summary = build_repo_timeline({"commits": commits, "nodes": {}, "relationships": []},
                                       now=_NOW)
        if summary["commitsByWeek"]:
            entry = summary["commitsByWeek"][0]
            assert "week" in entry
            assert "count" in entry
