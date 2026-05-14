import sys

from app.appium_nodes.routers import nodes as _nodes
from app.appium_nodes.routers.nodes import *  # noqa: F403

sys.modules[__name__] = _nodes
