import sys
from typing import TYPE_CHECKING

from app.hosts import service_diagnostics as _service_diagnostics

if TYPE_CHECKING:
    from app.hosts.service_diagnostics import *  # noqa: F403

sys.modules[__name__] = _service_diagnostics
