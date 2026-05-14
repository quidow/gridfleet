import sys

from app.appium_nodes.services import capability_keys as _capability_keys
from app.appium_nodes.services.capability_keys import *  # noqa: F403

sys.modules[__name__] = _capability_keys
