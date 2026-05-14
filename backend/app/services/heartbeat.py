import sys

from app.appium_nodes.services import heartbeat as _heartbeat
from app.appium_nodes.services.heartbeat import *  # noqa: F403

sys.modules[__name__] = _heartbeat
