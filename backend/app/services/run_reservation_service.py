import sys

from app.runs import service_reservation as _service_reservation
from app.runs.service_reservation import *  # noqa: F403

sys.modules[__name__] = _service_reservation
