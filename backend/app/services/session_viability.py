import sys

from app.sessions import service_viability as _service_viability
from app.sessions.service_viability import *  # noqa: F403

sys.modules[__name__] = _service_viability
