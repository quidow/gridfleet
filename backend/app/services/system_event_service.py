import sys
from typing import TYPE_CHECKING

from app.events import service_system as _service_system

if TYPE_CHECKING:
    from app.events.service_system import *  # noqa: F403

sys.modules[__name__] = _service_system
