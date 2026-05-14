import sys

from app.devices import locking as _locking
from app.devices.locking import *  # noqa: F403

sys.modules[__name__] = _locking
