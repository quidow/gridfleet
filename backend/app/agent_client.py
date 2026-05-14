import sys
from typing import TYPE_CHECKING

from app.agent_comm import client as _client

if TYPE_CHECKING:
    from app.agent_comm.client import *  # noqa: F403

sys.modules[__name__] = _client
