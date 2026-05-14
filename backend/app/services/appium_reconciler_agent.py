import sys

from app.appium_nodes.services import reconciler_agent as _reconciler_agent
from app.appium_nodes.services.reconciler_agent import *  # noqa: F403

sys.modules[__name__] = _reconciler_agent
