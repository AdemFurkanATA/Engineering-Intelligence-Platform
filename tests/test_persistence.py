"""
tests/test_persistence.py

Unit tests for the goal-service persistence layer (Phase 3.1).

Only tests InMemoryGoalStore — PostgreSQLGoalStore requires a live DB
and is covered by integration tests (see docs/PHASE_3_PLAN.md).
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "goal-service"))

from models import Goal, GoalStatus, GoalType
from persistence import InMemoryGoalStore, _mask_dsn, create_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a new event loop (avoids loop reuse across tests)."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_goal(**kwargs) -> Goal:
    defaults = dict(goal_text="test goal", repository_id="repo-1")
    defaults.update(kwargs)
    return Goal(**defaults)


# ---------------------------------------------------------------------------
# InMemoryGoalStore tests
# ---------------------------------------------------------------------------

class TestInMemoryGoalStore:

    def setup_method(self):
        self.store = InMemoryGoalStore()

    def test_save_and_get(self):
        g = _make_goal()
        _run(self.store.save(g))
        loaded = _run(self.store.get(g.goal_id))
        assert loaded is g   # same object reference in memory

    def test_get_nonexistent_returns_none(self):
        result = _run(self.store.get("does-not-exist"))
        assert result is None

    def test_save_updates_existing(self):
        g = _make_goal()
        _run(self.store.save(g))
        g.status = GoalStatus.EXECUTING
        _run(self.store.save(g))   # upsert
        loaded = _run(self.store.get(g.goal_id))
        assert loaded.status == GoalStatus.EXECUTING

    def test_list_all_empty(self):
        results = _run(self.store.list_all())
        assert results == []

    def test_list_all_returns_all_goals(self):
        for _ in range(3):
            _run(self.store.save(_make_goal()))
        results = _run(self.store.list_all())
        assert len(results) == 3

    def test_list_all_filter_by_status(self):
        g1 = _make_goal()
        g1.status = GoalStatus.COMPLETED
        g2 = _make_goal()
        g2.status = GoalStatus.FAILED
        _run(self.store.save(g1))
        _run(self.store.save(g2))
        completed = _run(self.store.list_all(status="completed"))
        assert len(completed) == 1
        assert completed[0].goal_id == g1.goal_id

    def test_list_all_filter_by_goal_type(self):
        g1 = _make_goal()
        g1.goal_type = GoalType.RISK_ANALYSIS
        g2 = _make_goal()
        g2.goal_type = GoalType.IMPACT_ANALYSIS
        _run(self.store.save(g1))
        _run(self.store.save(g2))
        risk = _run(self.store.list_all(goal_type="risk_analysis"))
        assert len(risk) == 1
        assert risk[0].goal_type == GoalType.RISK_ANALYSIS

    def test_list_all_filter_by_repository_id(self):
        g1 = _make_goal(repository_id="repo-A")
        g2 = _make_goal(repository_id="repo-B")
        _run(self.store.save(g1))
        _run(self.store.save(g2))
        results = _run(self.store.list_all(repository_id="repo-A"))
        assert len(results) == 1
        assert results[0].repository_id == "repo-A"

    def test_list_all_limit(self):
        for _ in range(5):
            _run(self.store.save(_make_goal()))
        results = _run(self.store.list_all(limit=3))
        assert len(results) == 3

    def test_delete_existing(self):
        g = _make_goal()
        _run(self.store.save(g))
        deleted = _run(self.store.delete(g.goal_id))
        assert deleted is True
        assert _run(self.store.get(g.goal_id)) is None

    def test_delete_nonexistent_returns_false(self):
        deleted = _run(self.store.delete("ghost-id"))
        assert deleted is False

    def test_clear(self):
        for _ in range(3):
            _run(self.store.save(_make_goal()))
        self.store.clear()
        assert len(self.store) == 0

    def test_len(self):
        assert len(self.store) == 0
        _run(self.store.save(_make_goal()))
        assert len(self.store) == 1

    def test_backend_name(self):
        assert self.store.backend == "in-memory"

    def test_close_is_noop(self):
        _run(self.store.close())   # should not raise


# ---------------------------------------------------------------------------
# create_store factory tests
# ---------------------------------------------------------------------------

class TestCreateStoreFactory:

    def test_no_database_url_returns_in_memory(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        store = _run(create_store())
        assert store.backend == "in-memory"
        assert isinstance(store, InMemoryGoalStore)

    def test_database_url_set_but_asyncpg_missing_falls_back(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
        # Simulate asyncpg not installed
        import persistence
        original = persistence._ASYNCPG_AVAILABLE
        persistence._ASYNCPG_AVAILABLE = False
        try:
            store = _run(create_store())
            assert store.backend == "in-memory"
        finally:
            persistence._ASYNCPG_AVAILABLE = original


# ---------------------------------------------------------------------------
# _mask_dsn helper
# ---------------------------------------------------------------------------

class TestMaskDsn:

    def test_masks_password(self):
        masked = _mask_dsn("postgresql://user:secret@localhost:5432/db")
        assert "secret" not in masked
        assert "user" in masked
        assert "localhost" in masked

    def test_no_auth_unchanged(self):
        dsn = "postgresql://localhost/db"
        assert _mask_dsn(dsn) == dsn
