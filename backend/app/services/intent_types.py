import sys

from app.devices.services import intent_types as _intent_types
from app.devices.services.intent_types import *  # noqa: F403

sys.modules[__name__] = _intent_types
