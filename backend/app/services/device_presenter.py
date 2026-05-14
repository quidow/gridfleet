import sys

from app.devices.services import presenter as _presenter
from app.devices.services.presenter import *  # noqa: F403

sys.modules[__name__] = _presenter
