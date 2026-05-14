import sys
from typing import TYPE_CHECKING

from app.packs.services import desired_state as _desired_state

if TYPE_CHECKING:
    from app.packs.services.desired_state import *  # noqa: F403

sys.modules[__name__] = _desired_state
