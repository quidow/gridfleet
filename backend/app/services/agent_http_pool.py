import sys
from typing import TYPE_CHECKING

from app.agent_comm import http_pool as _http_pool

if TYPE_CHECKING:
    from app.agent_comm.http_pool import *  # noqa: F403

sys.modules[__name__] = _http_pool
