import sys
from typing import TYPE_CHECKING

from app.agent_comm import probe_result as _probe_result

if TYPE_CHECKING:
    from app.agent_comm.probe_result import *  # noqa: F403

sys.modules[__name__] = _probe_result
