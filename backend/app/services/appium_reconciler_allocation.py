import sys

from app.appium_nodes.services import reconciler_allocation as _reconciler_allocation
from app.appium_nodes.services.reconciler_allocation import *  # noqa: F403

sys.modules[__name__] = _reconciler_allocation
