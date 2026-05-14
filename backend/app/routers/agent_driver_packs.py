import sys
from typing import TYPE_CHECKING

from app.packs.routers import agent_state as _agent_state

if TYPE_CHECKING:
    from app.packs.routers.agent_state import *  # noqa: F403

sys.modules[__name__] = _agent_state
