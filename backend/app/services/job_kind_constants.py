import sys
from typing import TYPE_CHECKING

from app.jobs import kinds as _kinds

if TYPE_CHECKING:
    from app.jobs.kinds import *  # noqa: F403

sys.modules[__name__] = _kinds
