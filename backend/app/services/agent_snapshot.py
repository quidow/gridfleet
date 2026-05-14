import sys
from typing import TYPE_CHECKING

from app.agent_comm import snapshot as _snapshot

if TYPE_CHECKING:
    from app.agent_comm.snapshot import *  # noqa: F403

sys.modules[__name__] = _snapshot
