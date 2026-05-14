import sys

from app.devices.services import write as _write
from app.devices.services.write import *  # noqa: F403

sys.modules[__name__] = _write
