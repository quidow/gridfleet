import sys
from typing import TYPE_CHECKING

from app.agent_comm import reconfigure_delivery as _reconfigure_delivery

if TYPE_CHECKING:
    from app.agent_comm.reconfigure_delivery import *  # noqa: F403

sys.modules[__name__] = _reconfigure_delivery
