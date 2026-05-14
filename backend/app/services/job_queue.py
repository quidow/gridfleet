import sys
from typing import TYPE_CHECKING

from app.jobs import queue as _queue

if TYPE_CHECKING:
    from app.jobs.queue import *  # noqa: F403

sys.modules[__name__] = _queue
