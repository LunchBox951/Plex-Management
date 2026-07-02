"""Regression tests for the ``_import_locks`` registry (issue #13).

The registry must stay self-bounded (a ``WeakValueDictionary``) without
reintroducing the pop-races-a-waiter hazard a naive eviction would cause.
"""

from __future__ import annotations

import asyncio

from plex_manager.services import import_service


async def test_import_lock_registry_self_evicts_after_release() -> None:
    for download_id in range(5):
        async with import_service._import_lock(  # pyright: ignore[reportPrivateUsage]
            download_id
        ):
            pass

    # CPython's immediate refcounting drops each weak-value entry once the
    # `async with` block's own temporary reference is the sole thing keeping
    # it alive and that block has exited -- no gc.collect() needed.
    assert len(import_service._import_locks) == 0  # pyright: ignore[reportPrivateUsage]


async def test_import_lock_is_the_same_object_for_concurrent_waiters() -> None:
    # The classic hazard a naive pop()-after-release would reintroduce: a
    # coroutine still awaiting the lock must see the SAME Lock instance a
    # concurrent holder is using, not a fresh one that lost mutual exclusion.
    download_id = 42
    order: list[str] = []
    holder_ready = asyncio.Event()
    release_holder = asyncio.Event()

    async def _holder() -> None:
        async with import_service._import_lock(  # pyright: ignore[reportPrivateUsage]
            download_id
        ):
            order.append("holder-acquired")
            holder_ready.set()
            await release_holder.wait()
            order.append("holder-released")

    async def _waiter() -> None:
        await holder_ready.wait()
        async with import_service._import_lock(  # pyright: ignore[reportPrivateUsage]
            download_id
        ):
            order.append("waiter-acquired")

    holder_task = asyncio.create_task(_holder())
    waiter_task = asyncio.create_task(_waiter())
    await holder_ready.wait()
    # Give the waiter a tick to block on the SAME lock instance.
    await asyncio.sleep(0)
    release_holder.set()
    await asyncio.gather(holder_task, waiter_task)

    assert order == ["holder-acquired", "holder-released", "waiter-acquired"]
    assert len(import_service._import_locks) == 0  # pyright: ignore[reportPrivateUsage]
