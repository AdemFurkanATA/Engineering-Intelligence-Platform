"""
Tests for git-analyzer-service job lifecycle state machine.

All tests use mocked Kafka and git operations — no real network/git needed.
"""
import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Paths set up by conftest.py
# git_analyzer_module fixture provided by conftest.py


@pytest.fixture(autouse=True)
def clear_jobs(git_analyzer_module):
    """Clear job store before each test."""
    git_analyzer_module._jobs.clear()
    yield
    git_analyzer_module._jobs.clear()


@pytest.fixture(autouse=True)
def init_semaphore(git_analyzer_module):
    """Ensure semaphore is initialised for tests that call _run_job."""
    import asyncio
    if git_analyzer_module._job_semaphore is None:
        git_analyzer_module._job_semaphore = asyncio.Semaphore(
            git_analyzer_module.GIT_MAX_CONCURRENT
        )


@pytest.fixture
def m(git_analyzer_module):
    return git_analyzer_module


@pytest.fixture
def client(m):
    from fastapi.testclient import TestClient
    return TestClient(m.app)


# ===========================================================================
# 1. Job creation & store
# ===========================================================================

class TestJobCreation:
    def test_make_job_creates_queued_state(self, m):
        job = m._make_job("repo-1", "https://github.com/x/y", "main", "org-1")
        assert job.status == "queued"
        assert job.repo_id == "repo-1"
        assert job.url == "https://github.com/x/y"
        assert job.branch == "main"
        assert job.org_id == "org-1"
        assert job.attempt == 0
        assert job.error is None
        assert job.since_sha is None
        assert job.started_at is None
        assert job.finished_at is None

    def test_make_job_generates_uuid(self, m):
        j1 = m._make_job("r1", "url1", "main", "org")
        j2 = m._make_job("r2", "url2", "main", "org")
        assert j1.job_id != j2.job_id
        uuid.UUID(j1.job_id)  # raises if invalid UUID

    def test_make_job_with_since_sha(self, m):
        job = m._make_job("r", "url", "main", "org", since_sha="abc123")
        assert job.since_sha == "abc123"

    def test_job_stored_in_dict(self, m):
        job = m._make_job("repo-x", "url", "main", "org")
        assert job.job_id in m._jobs
        assert m._jobs[job.job_id] is job

    def test_trim_jobs_removes_oldest(self, m):
        original_max = m._JOB_MAX
        m._JOB_MAX = 3
        try:
            for i in range(5):
                m._make_job(f"repo-{i}", f"url-{i}", "main", "org")
            m._trim_jobs()
            assert len(m._jobs) <= 3
        finally:
            m._JOB_MAX = original_max

    def test_job_status_enum_values(self, m):
        assert m.JobStatus.QUEUED    == "queued"
        assert m.JobStatus.RUNNING   == "running"
        assert m.JobStatus.SUCCEEDED == "succeeded"
        assert m.JobStatus.FAILED    == "failed"


# ===========================================================================
# 2. POST /analyze endpoint
# ===========================================================================

class TestAnalyzeEndpoint:
    def test_post_analyze_returns_202(self, m, client):
        with patch.object(m, "_run_job", new=AsyncMock()), \
             patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-abc",
                "url": "https://github.com/test/repo",
                "organizationId": "org-1",
            })
        assert r.status_code == 202

    def test_post_analyze_returns_job_id(self, m, client):
        with patch.object(m, "_run_job", new=AsyncMock()), \
             patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-b",
                "url": "https://github.com/test/repo",
                "organizationId": "org-1",
            })
        body = r.json()
        assert "jobId" in body
        assert body["status"] == "queued"
        assert body["repositoryId"] == "repo-b"

    def test_post_analyze_message_present(self, m, client):
        with patch.object(m, "_run_job", new=AsyncMock()), \
             patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-msg",
                "url": "https://github.com/x/y",
                "organizationId": "org",
            })
        assert "jobId" in r.json()["message"].lower() or "job" in r.json()["message"].lower()

    def test_post_analyze_deduplication_queued(self, m, client):
        """Second request for same repo while first is queued → 409."""
        m._make_job("repo-dup", "https://x.com/r", "main", "org")
        with patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-dup",
                "url": "https://x.com/r",
                "organizationId": "org",
            })
        assert r.status_code == 409
        assert "already" in r.json()["detail"].lower()

    def test_post_analyze_deduplication_running(self, m, client):
        """Second request for same repo while first is running → 409."""
        job = m._make_job("repo-run", "https://x.com/r", "main", "org")
        job.status = m.JobStatus.RUNNING
        with patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-run",
                "url": "https://x.com/r",
                "organizationId": "org",
            })
        assert r.status_code == 409

    def test_post_analyze_allows_new_job_after_succeeded(self, m, client):
        """Completed jobs should not block new submissions."""
        job = m._make_job("repo-ok", "https://x.com/r", "main", "org")
        job.status = m.JobStatus.SUCCEEDED
        with patch.object(m, "_run_job", new=AsyncMock()), \
             patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-ok",
                "url": "https://x.com/r",
                "organizationId": "org",
            })
        assert r.status_code == 202

    def test_post_analyze_allows_new_job_after_failed(self, m, client):
        """Failed jobs should not block new submissions."""
        job = m._make_job("repo-fail", "https://x.com/r", "main", "org")
        job.status = m.JobStatus.FAILED
        with patch.object(m, "_run_job", new=AsyncMock()), \
             patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-fail",
                "url": "https://x.com/r",
                "organizationId": "org",
            })
        assert r.status_code == 202

    def test_post_analyze_with_since_sha(self, m, client):
        with patch.object(m, "_run_job", new=AsyncMock()), \
             patch.object(m, "_fetch_last_sha", new=AsyncMock(return_value=None)):
            r = client.post("/analyze", json={
                "repositoryId": "repo-inc",
                "url": "https://github.com/x/y",
                "organizationId": "org",
                "sinceSha": "deadbeef",
            })
        assert r.status_code == 202
        assert r.json()["sinceSha"] == "deadbeef"

    def test_post_analyze_uses_graph_service_sha(self, m, client):
        """If sinceSha not in body, should try to fetch from graph-service."""
        with patch.object(m, "_run_job", new=AsyncMock()), \
             patch.object(m, "_fetch_last_sha",
                          new=AsyncMock(return_value="fetched-sha-abc")):
            r = client.post("/analyze", json={
                "repositoryId": "repo-sha",
                "url": "https://github.com/x/y",
                "organizationId": "org",
            })
        assert r.status_code == 202
        assert r.json()["sinceSha"] == "fetched-sha-abc"


# ===========================================================================
# 3. Job status endpoints
# ===========================================================================

class TestJobStatusEndpoints:
    def test_get_job_returns_job_details(self, m, client):
        job = m._make_job("r1", "url", "main", "org")
        r = client.get(f"/jobs/{job.job_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["job_id"] == job.job_id
        assert body["status"] == "queued"
        assert body["repo_id"] == "r1"

    def test_get_job_404_for_missing(self, client):
        r = client.get("/jobs/nonexistent-id")
        assert r.status_code == 404

    def test_list_jobs_returns_all(self, m, client):
        m._make_job("r1", "url1", "main", "org")
        m._make_job("r2", "url2", "main", "org")
        r = client.get("/jobs")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert len(body["jobs"]) == 2

    def test_list_jobs_filter_by_status(self, m, client):
        m._make_job("r1", "url1", "main", "org")  # queued
        j2 = m._make_job("r2", "url2", "main", "org")
        j2.status = m.JobStatus.SUCCEEDED
        r = client.get("/jobs?status=queued")
        body = r.json()
        assert all(j["status"] == "queued" for j in body["jobs"])

    def test_list_jobs_limit(self, m, client):
        for i in range(10):
            m._make_job(f"r{i}", f"url{i}", "main", "org")
        r = client.get("/jobs?limit=3")
        assert len(r.json()["jobs"]) == 3

    def test_delete_completed_job(self, m, client):
        job = m._make_job("r1", "url", "main", "org")
        job.status = m.JobStatus.SUCCEEDED
        r = client.delete(f"/jobs/{job.job_id}")
        assert r.status_code == 200
        assert job.job_id not in m._jobs

    def test_delete_queued_job(self, m, client):
        job = m._make_job("r1", "url", "main", "org")
        r = client.delete(f"/jobs/{job.job_id}")
        assert r.status_code == 200

    def test_delete_running_job_cancels_and_returns_200(self, m, client):
        """DELETE on a running job now cancels it (returns 200) instead of 409.

        The old 409 guard was replaced with real task cancellation via the
        _tasks registry. Since tests don't register actual asyncio tasks,
        the cancel path is a no-op here but the job is still removed.
        """
        job = m._make_job("r1", "url", "main", "org")
        job.status = m.JobStatus.RUNNING
        r = client.delete(f"/jobs/{job.job_id}")
        assert r.status_code == 200
        data = r.json()
        assert "cancelled" in data

    def test_delete_nonexistent_job_returns_404(self, client):
        r = client.delete("/jobs/no-such-id")
        assert r.status_code == 404

    def test_health_shows_job_counts(self, m, client):
        j1 = m._make_job("r1", "url", "main", "org")
        j2 = m._make_job("r2", "url", "main", "org")
        j2.status = m.JobStatus.RUNNING
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["jobs"]["total"] == 2
        assert body["jobs"]["running"] == 1
        assert body["jobs"]["queued"] == 1

    def test_health_shows_config(self, m, client):
        r = client.get("/health")
        body = r.json()
        assert "maxConcurrent" in body
        assert "maxRetries" in body
        assert body["version"] == "2.0.0"


# ===========================================================================
# 4. _run_job state transitions
# ===========================================================================

class TestRunJobStateTransitions:
    @pytest.fixture(autouse=True)
    def init_sem(self, m):
        m._job_semaphore = asyncio.Semaphore(m.GIT_MAX_CONCURRENT)

    def test_successful_job_transitions_to_succeeded(self, m):
        job = m._make_job("r", "url", "main", "org")
        with patch.object(m, "_clone_and_analyze", new=AsyncMock()):
            asyncio.get_event_loop().run_until_complete(m._run_job(job))
        assert job.status == "succeeded"
        assert job.finished_at is not None
        assert job.error is None

    def test_failed_job_transitions_to_failed(self, m):
        job = m._make_job("r", "url", "main", "org")
        original = m.GIT_MAX_RETRIES
        m.GIT_MAX_RETRIES = 0
        try:
            with patch.object(m, "_clone_and_analyze",
                              new=AsyncMock(side_effect=RuntimeError("boom"))), \
                 patch.object(m.asyncio, "sleep", new=AsyncMock()):
                asyncio.get_event_loop().run_until_complete(m._run_job(job))
        finally:
            m.GIT_MAX_RETRIES = original
        assert job.status == "failed"
        assert "boom" in (job.error or "")
        assert job.finished_at is not None

    def test_timeout_transitions_to_failed(self, m):
        job = m._make_job("r", "url", "main", "org")
        original = m.GIT_MAX_RETRIES
        original_timeout = m.GIT_CLONE_TIMEOUT_SEC
        m.GIT_MAX_RETRIES = 0
        m.GIT_CLONE_TIMEOUT_SEC = 0  # immediate timeout

        async def slow(*a, **kw):
            await asyncio.sleep(9999)

        try:
            with patch.object(m, "_clone_and_analyze", new=slow), \
                 patch.object(m.asyncio, "sleep", new=AsyncMock()):
                asyncio.get_event_loop().run_until_complete(m._run_job(job))
        finally:
            m.GIT_MAX_RETRIES = original
            m.GIT_CLONE_TIMEOUT_SEC = original_timeout
        assert job.status == "failed"
        assert "timeout" in (job.error or "").lower()

    def test_attempt_counter_increments(self, m):
        job = m._make_job("r", "url", "main", "org")
        original = m.GIT_MAX_RETRIES
        m.GIT_MAX_RETRIES = 1
        try:
            with patch.object(m, "_clone_and_analyze",
                              new=AsyncMock(side_effect=RuntimeError("err"))), \
                 patch.object(m.asyncio, "sleep", new=AsyncMock()):
                asyncio.get_event_loop().run_until_complete(m._run_job(job))
        finally:
            m.GIT_MAX_RETRIES = original
        assert job.attempt == 2  # initial + 1 retry

    def test_succeeded_job_has_no_error(self, m):
        job = m._make_job("r", "url", "main", "org")
        with patch.object(m, "_clone_and_analyze", new=AsyncMock()):
            asyncio.get_event_loop().run_until_complete(m._run_job(job))
        assert job.error is None
