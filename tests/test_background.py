import asyncio
import logging

from keel.background import Background


async def test_a_spawned_task_runs_and_close_cancels_what_is_left():
    background = Background(close_timeout_s=1.0)
    done = asyncio.Event()

    async def finish():
        done.set()

    assert background.spawn(finish(), "keel-finish")
    await done.wait()

    started = asyncio.Event()

    async def linger():
        started.set()
        await asyncio.sleep(3600)

    assert background.spawn(linger(), "keel-linger")
    await started.wait()
    await background.close()
    assert all(task.done() for task in background._tasks)


async def test_past_the_limit_the_task_is_dropped(caplog):
    background = Background(limit=1)
    hold = asyncio.Event()

    async def linger():
        await hold.wait()

    assert background.spawn(linger(), "keel-one")
    ran = False

    async def extra():
        nonlocal ran
        ran = True

    with caplog.at_level(logging.WARNING):
        assert background.spawn(extra(), "keel-two") is False
    await asyncio.sleep(0)  # a dropped coroutine must not warn about never being awaited
    assert ran is False and "keel-two" in caplog.text
    hold.set()
    await background.close()


async def test_a_failing_task_is_logged_and_does_not_escape(caplog):
    background = Background()

    async def boom():
        raise RuntimeError("nope")

    with caplog.at_level(logging.WARNING):
        assert background.spawn(boom(), "keel-boom")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert "nope" in caplog.text
    await background.close()
