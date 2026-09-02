"""
src/goal-service/executor.py

Async plan executor.

Iterates over a Plan's steps, calls each step's HTTP endpoint on the
appropriate backing service, and records the result (or error) on the step.

Behaviour:
  - Steps are executed sequentially (not in parallel) to keep the dependency
    between steps explicit and the output predictable.
  - A step marked required=True that fails will abort execution and mark the
    plan as failed.
  - A step marked required=False that fails is recorded as "skipped" with a
    warning; execution continues.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from models import Plan, PlanStep, StepStatus

logger = logging.getLogger(__name__)

# Timeout per step in seconds
_STEP_TIMEOUT = 30.0


async def execute_plan(
    plan: Plan,
    service_urls: Dict[str, str],
) -> tuple[bool, Optional[str]]:
    """Execute all steps in a plan, mutating step.status / step.result in place.

    Parameters
    ----------
    plan         : Plan to execute.
    service_urls : Mapping of service name → base URL, e.g.
                   {"graph-service": "http://graph-service:8005"}

    Returns
    -------
    (success, error_message)
    success is True if all required steps completed successfully.
    """
    async with httpx.AsyncClient(timeout=_STEP_TIMEOUT) as client:
        for step in plan.steps:
            step.status = StepStatus.RUNNING
            t0 = time.monotonic()
            try:
                result = await _call_step(client, step, service_urls)
                step.result      = result
                step.status      = StepStatus.COMPLETED
                step.duration_ms = int((time.monotonic() - t0) * 1000)
                logger.info("Step '%s' completed in %dms", step.name, step.duration_ms)

            except httpx.HTTPStatusError as exc:
                step.duration_ms = int((time.monotonic() - t0) * 1000)
                is_not_found = exc.response.status_code == 404

                if is_not_found and not step.required:
                    # Optional step — entity not yet in graph. Treat as no data.
                    step.result  = {}
                    step.status  = StepStatus.COMPLETED
                    logger.debug("Step '%s': 404 on optional step — using empty result", step.name)
                    continue

                step.error = str(exc)[:500]
                logger.warning("Step '%s' HTTP error: %s", step.name, step.error)

                if step.required:
                    step.status = StepStatus.FAILED
                    if is_not_found:
                        return False, (
                            f"Required step '{step.name}' returned 404 — "
                            f"repository/entity not found in the knowledge graph. "
                            f"Ensure the repository has been synced via POST /analyze first."
                        )
                    return False, f"Required step '{step.name}' failed: {step.error}"
                else:
                    step.status = StepStatus.SKIPPED
                    continue

            except Exception as exc:
                step.duration_ms = int((time.monotonic() - t0) * 1000)
                step.error = str(exc)[:500]
                logger.warning("Step '%s' failed: %s", step.name, step.error)

                if step.required:
                    step.status = StepStatus.FAILED
                    return False, f"Required step '{step.name}' failed: {step.error}"
                else:
                    step.status = StepStatus.SKIPPED
                    continue

    return True, None


async def _call_step(
    client: httpx.AsyncClient,
    step: PlanStep,
    service_urls: Dict[str, str],
) -> Any:
    """Make the HTTP GET call for a step and return the parsed JSON body.

    404 behaviour:
    - ``required=True`` steps: raises ``httpx.HTTPStatusError`` so the plan
      executor can propagate the failure.  A 404 on a required step means the
      repository/entity does not exist in the graph — this is a real error
      that should surface in the goal report rather than silently producing
      an empty report.
    - ``required=False`` steps: returns ``{}`` so execution continues.
      The report builder treats missing optional data as "no info available".

    The caller (_call_step) does not know the step's ``required`` flag, so we
    raise for all 4xx/5xx and let ``execute_plan`` decide based on
    ``step.required``.
    """
    base_url = service_urls.get(step.service, "")
    if not base_url:
        raise ValueError(f"No URL configured for service '{step.service}'")

    url = f"{base_url.rstrip('/')}{step.endpoint}"

    # Query params
    if step.params:
        query = urlencode({k: v for k, v in step.params.items() if v is not None})
        url = f"{url}?{query}"

    logger.debug("Calling: GET %s", url)
    resp = await client.get(url)

    if resp.status_code == 404:
        # Propagate — let execute_plan decide based on step.required
        resp.raise_for_status()

    resp.raise_for_status()
    return resp.json()

