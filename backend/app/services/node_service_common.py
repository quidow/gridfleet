import sys

from app.appium_nodes.services import common as _common
from app.appium_nodes.services.common import *  # noqa: F403

sys.modules[__name__] = _common
