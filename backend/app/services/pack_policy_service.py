import sys
from typing import TYPE_CHECKING

from app.packs.services import policy as _policy

if TYPE_CHECKING:
    from app.packs.services.policy import *  # noqa: F403

sys.modules[__name__] = _policy
