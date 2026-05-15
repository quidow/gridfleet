from __future__ import annotations

import logging
import threading

from agent_app.logs.handlers import RingBufferHandler


def test_emit_formats_and_evicts() -> None:
    handler = RingBufferHandler(maxlen=3)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("ring.test")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    for i in range(5):
        logger.info("line %d", i)
    snapshot = handler.snapshot()
    assert len(snapshot) == 3
    assert snapshot[-1].endswith("line 4")
    assert snapshot[0].endswith("line 2")


def test_stores_strings_not_records() -> None:
    handler = RingBufferHandler(maxlen=2)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("ring.test.2")
    logger.handlers = [handler]
    logger.error("boom", exc_info=False)
    snapshot = handler.snapshot()
    assert all(isinstance(item, str) for item in snapshot)


def test_concurrent_append_is_safe() -> None:
    handler = RingBufferHandler(maxlen=10_000)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("ring.test.3")
    logger.handlers = [handler]

    def writer(start: int) -> None:
        for i in range(start, start + 1_000):
            logger.info("x %d", i)

    threads = [threading.Thread(target=writer, args=(j * 1_000,)) for j in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = handler.snapshot()
    assert len(snapshot) == 4_000
