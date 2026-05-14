import sys

from app.appium_nodes.services import reconciler_convergence as _reconciler_convergence
from app.appium_nodes.services.reconciler_convergence import *  # noqa: F403

sys.modules[__name__] = _reconciler_convergence
