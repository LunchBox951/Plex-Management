"""Shared test-only helpers.

Keep this module dependency-free (pytest + stdlib only) so any test package can
import it without pulling in fixtures scoped to one area of the suite.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


async def assert_task_raises(task: asyncio.Task[Any], exc_type: type[BaseException]) -> None:
    """Await ``task`` and assert that awaiting it raises ``exc_type``.

    Use this instead of the bare idiom

        with pytest.raises(exc_type):
            await task

    whenever ``task`` is a plain ``asyncio.Task`` reference (a bare name or a
    subscript, not a fresh call expression). That bare form is what CodeQL's
    ``py/ineffectual-statement`` query flags as a false positive: alerts
    #351-#354 (dismissed off PR #375, tracked by issue #378) all fired on
    ``await <task-or-tasks[i]>`` as the sole statement inside a ``with
    pytest.raises(...)`` block. Statically the query sees an ``Expr`` statement
    wrapping an ``Await`` of a bare Name/Subscript and calls it a
    discarded-value expression — it doesn't special-case ``await`` the way it
    special-cases an ordinary function call (which may have side effects and is
    never flagged).

    This helper keeps the runtime semantics byte-for-byte identical — the task
    is still awaited, and whatever it raises (``CancelledError`` on
    cancellation, or a typed error such as ``PlexLibraryError``) still
    propagates and is asserted by ``pytest.raises`` — while presenting CodeQL
    with an assignment statement instead of a bare expression statement, which
    the query does not flag. Cancellation and typed-error propagation remain
    directly asserted: nothing here weakens or swallows the exception.

    Do not "fix" the false positive by suppressing the query instead: a path-
    or query-level suppression broad enough to cover this pattern would also
    hide genuine ineffectual statements elsewhere in the same files.
    """
    with pytest.raises(exc_type):
        _ = await task
