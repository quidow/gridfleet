import sys

from app.appium_nodes.services import heartbeat_outcomes as _heartbeat_outcomes
from app.appium_nodes.services.heartbeat_outcomes import *  # noqa: F403

sys.modules[__name__] = _heartbeat_outcomes
