import asyncio

from app.core.concurrency import per_key_semaphores


async def test_per_key_semaphores_bounds_each_key_independently() -> None:
    sems = per_key_semaphores(1)
    running = {"a": 0, "b": 0}
    peak = {"a": 0, "b": 0}

    async def work(key: str) -> None:
        async with sems[key]:
            running[key] += 1
            peak[key] = max(peak[key], running[key])
            await asyncio.sleep(0)
            running[key] -= 1

    await asyncio.gather(*[work("a") for _ in range(5)], *[work("b") for _ in range(5)])
    assert peak == {"a": 1, "b": 1}
