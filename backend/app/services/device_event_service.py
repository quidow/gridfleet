import sys

from app.devices.services import event as _event
from app.devices.services.event import *  # noqa: F403

sys.modules[__name__] = _event
