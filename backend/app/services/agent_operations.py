import sys
from typing import TYPE_CHECKING

from app.agent_comm import operations as _operations

if TYPE_CHECKING:
    from app.agent_comm.operations import *  # noqa: F403

sys.modules[__name__] = _operations
