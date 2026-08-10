"""Detached (fire-and-forget) work spawned from a request handler.

Some work belongs to a request but must not be ON it: entitlement capture at
sign-in (#484 PR-3) talks to the Plex server, which may be a LAN address that
black-holes, and a 30-second client timeout on the sign-in path would stall the
one endpoint an operator needs working when things are broken.

Two hazards this module exists to close, both easy to get wrong with a bare
``asyncio.create_task``:

* **Orphaning.** The event loop holds only a WEAK reference to a running task,
  so a task nobody keeps a reference to can be garbage-collected mid-flight and
  simply vanish. Every task spawned here is held in a set on ``app.state`` until
  it finishes.
* **Silent death.** A fire-and-forget task that raises would otherwise surface
  only as an "exception was never retrieved" warning at GC time, if at all. The
  done-callback retrieves and logs it (north star #3).

Deliberately NOT a general job runner: no retries, no ordering, no persistence.
Anything that must survive a restart belongs in a background LOOP
(``web/app.py``) that re-derives its work from the database, not here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from fastapi import FastAPI

__all__ = ["detached_tasks", "spawn_detached"]

_logger = logging.getLogger(__name__)

_STATE_ATTR = "detached_tasks"


def detached_tasks(app: FastAPI) -> set[asyncio.Task[None]]:
    """The app's live detached tasks, created lazily.

    Lazy so a handler that spawns work still behaves in tests that build a bare
    ``FastAPI()`` without going through ``lifespan``.
    """
    tasks = cast("set[asyncio.Task[None]] | None", getattr(app.state, _STATE_ATTR, None))
    if tasks is None:
        fresh: set[asyncio.Task[None]] = set()
        setattr(app.state, _STATE_ATTR, fresh)
        return fresh
    return tasks


def spawn_detached(
    app: FastAPI, coro: Coroutine[Any, Any, None], *, name: str
) -> asyncio.Task[None]:
    """Run ``coro`` off the request path, held and logged. Never raises.

    The returned task is primarily for tests (await it to observe the work);
    production callers fire and forget. ``name`` appears in the failure log and
    in task introspection.
    """
    task = asyncio.create_task(coro, name=name)
    tasks = detached_tasks(app)
    tasks.add(task)

    def _finish(done: asyncio.Task[None]) -> None:
        tasks.discard(done)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            _logger.error("detached task %r failed", name, exc_info=exc)

    task.add_done_callback(_finish)
    return task
