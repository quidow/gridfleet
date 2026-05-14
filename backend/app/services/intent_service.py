import sys

from app.devices.services import intent as _intent
from app.devices.services.intent import *  # noqa: F403

sys.modules[__name__] = _intent
