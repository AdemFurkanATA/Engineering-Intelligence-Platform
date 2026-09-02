"""
Tests for clone scenario edge cases in git-analyzer-service.

All git operations are mocked via the fake 'git' module injected by conftest.py.
No real network or git binary required.

Covers:
- Invalid URL → RepositoryCloneFailed event
- Auth failure → RepositoryCloneFailed event
- Finally-block cleanup on exception
- Incremental sync: since_sha commit filtering
- Branch parameter forwarded to Repo.clone_from
- _fetch_last_sha helper
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest.py injects fake git into sys.modules and provides git_analyzer_module fixture


def _git():
    """Return the fake git module injected by conftest.py."""
    return sys.modules["git"]


# ---------------------------------------------------------------------------
# Shared fixtures (test-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture
def m(git_analyzer_module):
    """Convenience alias used by all tests in this file."""
    return git_analyzer_module


@pytest.fixture(autouse=True)
def reset_between_tests(git_analyzer_module):
    """Clear state between tests."""
    git_analyzer_module._jobs.clear()
    # Reset fake git Repo mock
    _git().Repo.clone_from.reset_mock()
    _git().Repo.clone_from.side_effect = None
    _git().Repo.clone_from.return_value = MagicMock()
    yield
    git_analyzer_module._jobs.clear()


@pytest.fixture
def published(git_analyzer_module):
    """Capture events published by the service."""
    events = []

    async def fake_publish(topic, event):
        events.append({"topic": topic, "event": event})

    git_analyzer_module.publisher.publish = fake_publish
    return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_commit(sha, email="dev@example.com", name="Dev",
                 message="fix bug", ts=1_700_000_000):
    c = MagicMock()
    c.hexsha         = sha
    c.author.email   = email
    c.author.name    = name
    c.message        = message
    c.committed_date = ts
    c.stats.files    = {"a.py": {}, "b.py": {}}
    return c


def _fake_repo(commits=None):
    repo = MagicMock()
    repo.iter_commits.return_value = commits or []
    repo.remotes.origin.fetch = MagicMock(return_value=None)
    return repo


def _payload(event):
    """Extract payload dict from an EventEnvelope or dict."""
    if hasattr(event, "model_dump"):
        return event.model_dump().get("payload", {})
    return (event if isinstance(event, dict) else {}).get("payload", {})


# ===========================================================================
# 1. Clone failure scenarios
# ===========================================================================

class TestCloneFailureScenarios:
    def test_invalid_url_publishes_clone_failed(self, m, published, tmp_path):
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.side_effect = Exception("repository not found")

        run(m._clone_and_analyze("repo-bad", "https://invalid.example/r.git", "org", "main"))

        topics = [e["topic"] for e in published]
        assert "repository.clone_failed" in topics

    def test_clone_failed_error_in_payload(self, m, published, tmp_path):
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.side_effect = Exception("Authentication failed for url")

        run(m._clone_and_analyze("repo-auth", "https://private.example/r.git", "org", "main"))

        failed = [e for e in published if e["topic"] == "repository.clone_failed"]
        assert len(failed) >= 1
        error_msg = _payload(failed[0]["event"]).get("error", "")
        assert "Authentication" in error_msg or "failed" in error_msg.lower()

    def test_no_cloned_event_on_failure(self, m, published, tmp_path):
        """If clone fails, RepositoryCloned must NOT be published."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.side_effect = Exception("timeout")

        run(m._clone_and_analyze("repo-timeout", "https://x.com/r", "org", "main"))

        assert "repository.cloned" not in [e["topic"] for e in published]

    def test_clone_dir_cleaned_after_failure(self, m, published, tmp_path):
        """After a clone failure the temp dir should not remain."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.side_effect = Exception("fail")

        run(m._clone_and_analyze("repo-clean", "https://x.com/r", "org", "main"))

        clone_dir = tmp_path / "eip-repo-clean"
        assert not clone_dir.exists()

    def test_analysis_exception_still_cleans_dir(self, m, published, tmp_path):
        """If analysis raises after clone, the finally block still removes the dir."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)

        mock_repo = _fake_repo()
        mock_repo.iter_commits.side_effect = RuntimeError("disk error")
        _git().Repo.clone_from.side_effect = None
        _git().Repo.clone_from.return_value = mock_repo

        clone_dir = tmp_path / "eip-repo-mid"
        clone_dir.mkdir(parents=True, exist_ok=True)

        run(m._clone_and_analyze("repo-mid", "https://x.com/r", "org", "main"))

        assert not clone_dir.exists()


# ===========================================================================
# 2. Successful clone events
# ===========================================================================

class TestSuccessfulClone:
    def test_cloned_event_published_on_success(self, m, published, tmp_path):
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.return_value = _fake_repo(
            [_make_commit("sha-a"), _make_commit("sha-b")]
        )

        run(m._clone_and_analyze("repo-ok", "https://x.com/r", "org", "main"))

        assert "repository.cloned" in [e["topic"] for e in published]

    def test_dependency_events_published(self, m, published, tmp_path):
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.return_value = _fake_repo()

        dummy_dep = {"name": "requests", "version": "2.31",
                     "ecosystem": "pip", "sourceFile": "requirements.txt"}
        with patch("parsers.python_parser.parse_directory", return_value=[dummy_dep]), \
             patch("parsers.node_parser.parse_directory", return_value=[]), \
             patch("parsers.go_parser.parse_directory", return_value=[]), \
             patch("parsers.rust_parser.parse_directory", return_value=[]):
            run(m._clone_and_analyze("repo-dep", "https://x.com/r", "org", "main"))

        dep_events = [e for e in published if e["topic"] == "dependency.detected"]
        assert len(dep_events) == 1
        assert _payload(dep_events[0]["event"]).get("name") == "requests"

    def test_commit_events_published(self, m, published, tmp_path):
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        commits = [_make_commit(f"sha-{i}") for i in range(3)]
        _git().Repo.clone_from.return_value = _fake_repo(commits)

        run(m._clone_and_analyze("repo-commits", "https://x.com/r", "org", "main"))

        commit_events = [e for e in published if e["topic"] == "commit.analyzed"]
        assert len(commit_events) == 3


# ===========================================================================
# 3. Incremental sync (since_sha)
# ===========================================================================

class TestIncrementalSync:
    def test_since_sha_stops_at_known_commit(self, m, published, tmp_path):
        """Commits at since_sha and older must NOT be published."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)

        c_new1  = _make_commit("sha-new-001")
        c_new2  = _make_commit("sha-new-002")
        c_known = _make_commit("sha-known-stop")
        c_old   = _make_commit("sha-very-old")

        _git().Repo.clone_from.return_value = _fake_repo(
            [c_new1, c_new2, c_known, c_old]
        )

        run(m._clone_and_analyze(
            "repo-inc", "https://x.com/r", "org", "main",
            since_sha="sha-known-stop"
        ))

        shas = set()
        for e in published:
            if e["topic"] == "commit.analyzed":
                sha = _payload(e["event"]).get("sha")
                if sha:
                    shas.add(sha)

        assert "sha-new-001" in shas
        assert "sha-new-002" in shas
        assert "sha-known-stop" not in shas
        assert "sha-very-old" not in shas

    def test_no_since_sha_processes_all_commits(self, m, published, tmp_path):
        """Without since_sha, all commits are processed."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        commits = [_make_commit(f"sha-{i}") for i in range(4)]
        _git().Repo.clone_from.return_value = _fake_repo(commits)

        run(m._clone_and_analyze(
            "repo-full", "https://x.com/r", "org", "main", since_sha=None
        ))

        commit_events = [e for e in published if e["topic"] == "commit.analyzed"]
        assert len(commit_events) == 4

    def test_incremental_always_clones_since_sha_filters_commits(self, m, published, tmp_path):
        """since_sha filters which commits are published, but we always do a fresh clone.

        The fetch-on-existing-dir path was removed because clone_dir is always
        deleted in the finally block — a leftover dir from a previous run would
        be a filesystem artifact, not a managed cache.
        """
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        commits = [_make_commit("sha-new"), _make_commit("sha-old")]
        _git().Repo.clone_from.return_value = _fake_repo(commits)

        run(m._clone_and_analyze(
            "repo-fetch", "https://x.com/r", "org", "main",
            since_sha="sha-old"   # only sha-new should be published
        ))
        # clone_from IS called (always fresh clone)
        _git().Repo.clone_from.assert_called_once()
        # Only the newer commit (before sha-old stop point) is published
        commit_events = [e for e in published if e["topic"] == "commit.analyzed"]
        assert len(commit_events) == 1

    def test_since_sha_not_found_processes_all(self, m, published, tmp_path):
        """If since_sha is not in the commit list, all commits are processed."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        commits = [_make_commit("sha-a"), _make_commit("sha-b")]
        _git().Repo.clone_from.return_value = _fake_repo(commits)

        run(m._clone_and_analyze(
            "repo-nf", "https://x.com/r", "org", "main",
            since_sha="sha-nonexistent"
        ))

        commit_events = [e for e in published if e["topic"] == "commit.analyzed"]
        assert len(commit_events) == 2


# ===========================================================================
# 4. Branch parameter
# ===========================================================================

class TestBranchParameter:
    def test_branch_passed_to_clone_from(self, m, published, tmp_path):
        """The branch parameter must be forwarded to Repo.clone_from."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.return_value = _fake_repo()

        run(m._clone_and_analyze("repo-dev", "https://x.com/r", "org", "develop"))

        call_kwargs = _git().Repo.clone_from.call_args[1]
        assert call_kwargs.get("branch") == "develop"

    def test_main_branch_is_default(self, m, published, tmp_path):
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.return_value = _fake_repo()

        run(m._clone_and_analyze("repo-default", "https://x.com/r", "org"))

        call_kwargs = _git().Repo.clone_from.call_args[1]
        assert call_kwargs.get("branch") == "main"

    def test_branch_in_cloned_event_payload(self, m, published, tmp_path):
        """The defaultBranch field in RepositoryCloned payload matches requested branch."""
        m.GIT_CLONE_BASE_DIR = str(tmp_path)
        _git().Repo.clone_from.return_value = _fake_repo()

        run(m._clone_and_analyze("repo-br", "https://x.com/r", "org", "feature/xyz"))

        cloned = [e for e in published if e["topic"] == "repository.cloned"]
        assert len(cloned) == 1
        assert _payload(cloned[0]["event"]).get("defaultBranch") == "feature/xyz"


# ===========================================================================
# 5. _fetch_last_sha helper
# ===========================================================================

class TestFetchLastSha:
    def _mock_client(self, status, body):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        return instance

    def test_returns_sha_from_graph_service(self, m):
        instance = self._mock_client(200, {"lastAnalyzedSha": "abc123def456"})
        with patch("httpx.AsyncClient", return_value=instance):
            result = run(m._fetch_last_sha("repo-123"))
        assert result == "abc123def456"

    def test_returns_none_on_404(self, m):
        instance = self._mock_client(404, {})
        with patch("httpx.AsyncClient", return_value=instance):
            result = run(m._fetch_last_sha("repo-404"))
        assert result is None

    def test_returns_none_when_sha_missing_in_response(self, m):
        instance = self._mock_client(200, {"repositoryId": "r", "lastAnalyzedSha": None})
        with patch("httpx.AsyncClient", return_value=instance):
            result = run(m._fetch_last_sha("repo-null"))
        assert result is None

    def test_returns_none_on_network_error(self, m):
        import httpx
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=instance):
            result = run(m._fetch_last_sha("repo-down"))
        assert result is None

    def test_returns_none_on_timeout(self, m):
        import httpx
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=instance):
            result = run(m._fetch_last_sha("repo-slow"))
        assert result is None
