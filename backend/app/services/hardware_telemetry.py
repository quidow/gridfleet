import sys
from typing import TYPE_CHECKING

from app.hosts import service_hardware_telemetry as _service_hardware_telemetry

if TYPE_CHECKING:
    from app.hosts.service_hardware_telemetry import *  # noqa: F403

sys.modules[__name__] = _service_hardware_telemetry
