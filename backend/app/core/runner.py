"""Keeps long-running per-migration background tasks (bulk copy + sync, fallback
sync, reconciliation) alive and addressable, so an API call or a backend restart
can tell whether one is already running instead of starting a duplicate."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_tasks: dict[tuple[str, str], asyncio.Task] = {}


def is_running(migration_id: str, kind: str) -> bool:
    task = _tasks.get((str(migration_id), kind))
    return task is not None and not task.done()


def spawn(migration_id: str, kind: str, coro: Coroutine[Any, Any, Any]) -> bool:
    """Start `coro` unless a task of the same kind is already running for this
    migration. Returns False (and closes the coroutine) if one is."""
    key = (str(migration_id), kind)
    if is_running(*key):
        coro.close()
        return False
    task = asyncio.create_task(coro, name=f"{kind}:{migration_id}")
    _tasks[key] = task

    def _done(t: asyncio.Task) -> None:
        if _tasks.get(key) is t:
            _tasks.pop(key, None)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Background %s task for migration %s crashed", kind, migration_id,
                         exc_info=t.exception())

    task.add_done_callback(_done)
    return True


async def cancel_all() -> None:
    for task in list(_tasks.values()):
        task.cancel()
    await asyncio.gather(*_tasks.values(), return_exceptions=True)
    _tasks.clear()
