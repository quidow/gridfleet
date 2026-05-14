import sys
from typing import TYPE_CHECKING

from app.jobs import statuses as _statuses

if TYPE_CHECKING:
    from app.jobs.statuses import *  # noqa: F403

sys.modules[__name__] = _statuses
