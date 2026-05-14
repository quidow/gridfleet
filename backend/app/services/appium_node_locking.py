import sys

from app.appium_nodes.services import locking as _locking
from app.appium_nodes.services.locking import *  # noqa: F403

sys.modules[__name__] = _locking
