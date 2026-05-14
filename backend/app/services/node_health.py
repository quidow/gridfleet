import sys

from app.appium_nodes.services import node_health as _node_health
from app.appium_nodes.services.node_health import *  # noqa: F403

sys.modules[__name__] = _node_health
