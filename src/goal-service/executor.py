"""
src/goal-service/executor.py

Async plan executor — Phase 3.2: Retry + Parallel execution.

Improvements over Phase 3.0:
  - Retry: transient failures (5xx, connect error, timeout) are retried up to
    MAX_RETRIES times with exponential back-off.  4xx errors are NOT retried
    (client-side errors are deterministic).
  - Parallel execution: steps that share the same parallel_group string are
    gathered with asyncio.gather and run concurrently.  Steps with
    parallel_group=None are run sequentially (the safe default).
  - Step ordering: steps are first grouped maintaining their declared order;
    sequential runs happen in declaration order; within a parallel group all
    steps start simultaneously.

Behaviour:
  - A required step that exhausts all retries fails the plan.
  - An optional step that exhausts all retries is recorded as SKIPPED.
  - A 404 on a required step returns a clear actionable error message.
  - A 404 on an optional step results in an empty result ({}) so execution
    continues and the report builder treats it as "no data available".
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from models import Plan, PlanStep, StepStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

_STEP_TIMEOUT   = 30.0   # seconds per HTTP call
MAX_RETRIES     = 3      # max attempts for transient failures
_RETRY_BASE_S   = 0.5    # initial back-off: 0.5s, 1.0s, 2.0s

# HTTP status codes that are safe to retry (transient server-side errors)
_RETRYABLE_CODES = frozenset({500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def execute_plan(
    plan: Plan,
    service_urls: Dict[str, str],
) -> Tuple[bool, Optional[str]]:
    """Execute all steps in a plan, mutating step.status / step.result in place.

    Steps are executed in the order they appear in ``plan.steps``.
    Consecutive steps that share the same ``parallel_group`` value are
    gathered and executed concurrently.

    Parameters
    ----------
    plan         : Plan to execute.
    service_urls : Mapping of service name → base URL.

    Returns
    -------
    (success, error_message)
    success is True if all required steps completed successfully.
    """
    async with httpx.AsyncClient(timeout=_STEP_TIMEOUT) as client:
        # Collect steps into ordered batches: sequential steps are batches of
        # size 1; consecutive steps with the same non-None parallel_group are
        # collected into a single batch.
        batches: List[List[PlanStep]] = _make_batches(plan.steps)

        for batch in batches:
            if len(batch) == 1:
                # Sequential step
                ok, err = await _run_step(client, batch[0], service_urls)
                if not ok:
                    return False, err
            else:
                # Parallel batch — run concurrently, collect failures
                results = await asyncio.gather(
                    *[_run_step(client, s, service_urls) for s in batch],
                    return_exceptions=False,
                )
                for ok, err in results:
                    if not ok:
                        return False, err

    return True, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_batches(steps: List[PlanStep]) -> List[List[PlanStep]]:
    """Group consecutive steps with the same parallel_group into batches.

    Steps with parallel_group=None are always their own batch (sequential).
    """
    batches: List[List[PlanStep]] = []
    current_batch: List[PlanStep] = []
    current_group: Optional[str]  = None

    for step in steps:
        if step.parallel_group is None:
            # Flush any open parallel batch first
            if current_batch:
                batches.append(current_batch)
                current_batch = []
                current_group = None
            batches.append([step])
        elif step.parallel_group == current_group:
            current_batch.append(step)
        else:
            # New parallel group — flush previous batch
            if current_batch:
                batches.append(current_batch)
            current_batch = [step]
            current_group = step.parallel_group

    if current_batch:
        batches.append(current_batch)

    return batches


async def _run_step(
    client: httpx.AsyncClient,
    step: PlanStep,
    service_urls: Dict[str, str],
) -> Tuple[bool, Optional[str]]:
    """Execute one step with retry logic.

    Returns (True, None) on success or (False, error_msg) on unrecoverable failure.
    Mutates step in place (status, result, error, duration_ms, retries).
    """
    step.status = StepStatus.RUNNING
    t0 = time.monotonic()
    last_error: Optional[str] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = await _call_step(client, step, service_urls)
            step.result      = result
            step.status      = StepStatus.COMPLETED
            step.duration_ms = int((time.monotonic() - t0) * 1000)
            step.retries     = attempt - 1
            if attempt > 1:
                logger.info(
                    "Step '%s' succeeded on attempt %d/%d in %dms",
                    step.name, attempt, MAX_RETRIES, step.duration_ms,
                )
            else:
                logger.info(
                    "Step '%s' completed in %dms", step.name, step.duration_ms,
                )
            return True, None

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            is_not_found = status_code == 404
            is_retryable  = status_code in _RETRYABLE_CODES

            # 404 is never retried — it's deterministic
            if is_not_found:
                step.duration_ms = int((time.monotonic() - t0) * 1000)
                if not step.required:
                    step.result  = {}
                    step.status  = StepStatus.COMPLETED
                    logger.debug(
                        "Step '%s': 404 on optional step — using empty result", step.name
                    )
                    return True, None
                # Required step — 404 = repo not synced
                step.status = StepStatus.FAILED
                step.error  = str(exc)[:500]
                msg = (
                    f"Required step '{step.name}' returned 404 — "
                    f"repository/entity not found in the knowledge graph. "
                    f"Ensure the repository has been synced via POST /analyze first."
                )
                return False, msg

            last_error = f"HTTP {status_code}: {str(exc)[:300]}"

            if is_retryable and attempt < MAX_RETRIES:
                wait = _RETRY_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "Step '%s' got %d (attempt %d/%d) — retrying in %.1fs",
                    step.name, status_code, attempt, MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            # Non-retryable 4xx OR exhausted retries
            step.duration_ms = int((time.monotonic() - t0) * 1000)
            step.error       = last_error
            step.retries     = attempt - 1

            if step.required:
                step.status = StepStatus.FAILED
                return False, f"Required step '{step.name}' failed: {last_error}"
            else:
                step.status = StepStatus.SKIPPED
                return True, None

        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            last_error = str(exc)[:300]

            if attempt < MAX_RETRIES:
                wait = _RETRY_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "Step '%s' transient error (attempt %d/%d) — retrying in %.1fs: %s",
                    step.name, attempt, MAX_RETRIES, wait, last_error,
                )
                await asyncio.sleep(wait)
                continue

            # Exhausted retries
            step.duration_ms = int((time.monotonic() - t0) * 1000)
            step.error       = last_error
            step.retries     = attempt - 1

            if step.required:
                step.status = StepStatus.FAILED
                return False, f"Required step '{step.name}' failed after {MAX_RETRIES} retries: {last_error}"
            else:
                step.status = StepStatus.SKIPPED
                logger.warning(
                    "Optional step '%s' skipped after %d retries: %s",
                    step.name, MAX_RETRIES, last_error,
                )
                return True, None

        except Exception as exc:
            # Unexpected error — no retry
            step.duration_ms = int((time.monotonic() - t0) * 1000)
            step.error       = str(exc)[:500]
            step.retries     = attempt - 1

            if step.required:
                step.status = StepStatus.FAILED
                return False, f"Required step '{step.name}' failed: {step.error}"
            else:
                step.status = StepStatus.SKIPPED
                return True, None

    # Should not reach here but satisfies type checker
    step.status = StepStatus.FAILED
    return False, f"Step '{step.name}' exhausted retries"


async def _call_step(
    client: httpx.AsyncClient,
    step: PlanStep,
    service_urls: Dict[str, str],
) -> Any:
    """Make the HTTP GET call for a step and return the parsed JSON body.

    Raises httpx.HTTPStatusError for any non-2xx response so the caller can
    inspect the status code and decide whether to retry or abort.
    """
    base_url = service_urls.get(step.service, "")
    if not base_url:
        raise ValueError(f"No URL configured for service '{step.service}'")

    url = f"{base_url.rstrip('/')}{step.endpoint}"

    # Strip internal planner metadata keys (prefixed with '_') from query params
    query_params = {
        k: v for k, v in (step.params or {}).items()
        if v is not None and not k.startswith("_")
    }
    if query_params:
        url = f"{url}?{urlencode(query_params)}"

    logger.debug("Calling: GET %s", url)
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()
