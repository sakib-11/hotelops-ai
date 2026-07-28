"""Individual dependency health check functions.

Each check has a bounded timeout and returns a DependencyStatus.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from backend.app.infrastructure.health.models import DependencyStatus, TimeoutError

logger = logging.getLogger(__name__)


async def run_check(
    check_name: str,
    check_fn: Callable[[], Coroutine[Any, Any, bool]],
    timeout: float = 5.0,
) -> DependencyStatus:
    """Run a dependency check with a bounded timeout.

    Args:
        check_name: Human-readable name for logging.
        check_fn: Async callable returning True for healthy.
        timeout: Maximum seconds to wait for the check.

    Returns:
        DependencyStatus.OK, FAILED, or TIMEOUT.
    """
    try:
        result = await asyncio.wait_for(check_fn(), timeout=timeout)
        if result:
            return DependencyStatus.OK
        logger.warning("Dependency check '%s' returned unhealthy", check_name)
        return DependencyStatus.FAILED
    except TimeoutError:
        logger.warning("Dependency check '%s' timed out after %.1fs", check_name, timeout)
        return DependencyStatus.TIMEOUT
    except Exception:
        logger.exception("Dependency check '%s' raised an exception", check_name)
        return DependencyStatus.FAILED
