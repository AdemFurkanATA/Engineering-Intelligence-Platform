"""
conftest.py — Shared pytest fixtures for Engineering Intelligence Platform tests.

Handles module isolation for the git-analyzer-service, which has common-name
imports ('main') that can collide with sys.modules cache across test files.
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_ROOT     = os.path.dirname(os.path.dirname(__file__))
_SRC_DIR  = os.path.join(_ROOT, "src")
_SVC_DIR  = os.path.join(_ROOT, "src", "git-analyzer-service")

for _d in (_SRC_DIR, _SVC_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)


# ---------------------------------------------------------------------------
# Fake `git` module
# Injected into sys.modules so that any test-file that imports main from
# git-analyzer-service can do `import git` without gitpython installed.
# ---------------------------------------------------------------------------

def _inject_fake_git():
    if "git" in sys.modules:
        return sys.modules["git"]

    git_mod = types.ModuleType("git")

    class FakeGitCommandError(Exception):
        def __init__(self, command="clone", status=128, stderr="error"):
            self.command = command
            self.status  = status
            self.stderr  = stderr
            super().__init__(f"git {command} failed ({status}): {stderr}")

    exc_mod = types.ModuleType("git.exc")
    exc_mod.GitCommandError = FakeGitCommandError
    git_mod.exc             = exc_mod
    git_mod.GitCommandError = FakeGitCommandError
    git_mod.Repo            = MagicMock()

    sys.modules["git"]     = git_mod
    sys.modules["git.exc"] = exc_mod
    return git_mod


# Inject once at collection time
_fake_git = _inject_fake_git()


# ---------------------------------------------------------------------------
# git-analyzer-service module fixture
# Session-scoped so the module is only loaded once per test session,
# but _jobs is cleared per test via the clear_git_jobs fixture.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def git_analyzer_module():
    """Load git-analyzer-service main with Kafka fully mocked.

    Uses spec_from_file_location to load as 'git_analyzer_main' (not bare 'main')
    to avoid sys.modules collision with other services that also have a main.py.
    """
    import asyncio
    import importlib.util

    main_path = os.path.join(_SVC_DIR, "main.py")

    # Remove any stale entry to force a fresh load
    sys.modules.pop("git_analyzer_main", None)

    with patch("shared.kafka.EventPublisher", MagicMock), \
         patch("shared.kafka.EventSubscriber", MagicMock):
        spec = importlib.util.spec_from_file_location("git_analyzer_main", main_path)
        m = importlib.util.module_from_spec(spec)
        sys.modules["git_analyzer_main"] = m
        spec.loader.exec_module(m)
        m._job_semaphore = asyncio.Semaphore(m.GIT_MAX_CONCURRENT)
        return m


@pytest.fixture(autouse=False)
def clear_git_jobs(git_analyzer_module):
    """Clear the in-memory job store before and after each test."""
    git_analyzer_module._jobs.clear()
    yield
    git_analyzer_module._jobs.clear()


# Convenience alias for test files that fixture-name is 'm'
@pytest.fixture
def git_analyzer_m(git_analyzer_module):
    return git_analyzer_module
