import sys
from typing import TYPE_CHECKING

from app.agent_comm import error_codes as _error_codes

if TYPE_CHECKING:
    from app.agent_comm.error_codes import *  # noqa: F403

sys.modules[__name__] = _error_codes
