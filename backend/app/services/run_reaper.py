import sys

from app.runs import service_reaper as _service_reaper
from app.runs.service_reaper import *  # noqa: F403

sys.modules[__name__] = _service_reaper
