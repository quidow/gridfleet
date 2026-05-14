import sys

from app.runs import service as _service
from app.runs.service import *  # noqa: F403

sys.modules[__name__] = _service
