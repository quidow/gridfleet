import sys
from typing import TYPE_CHECKING

from app.agent_comm import models as _models

if TYPE_CHECKING:
    from app.agent_comm.models import *  # noqa: F403

sys.modules[__name__] = _models
