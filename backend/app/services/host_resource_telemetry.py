import sys
from typing import TYPE_CHECKING

from app.hosts import service_resource_telemetry as _service_resource_telemetry

if TYPE_CHECKING:
    from app.hosts.service_resource_telemetry import *  # noqa: F403

sys.modules[__name__] = _service_resource_telemetry
