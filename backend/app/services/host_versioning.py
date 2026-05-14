import sys
from typing import TYPE_CHECKING

from app.hosts import service_versioning as _service_versioning

if TYPE_CHECKING:
    from app.hosts.service_versioning import *  # noqa: F403

sys.modules[__name__] = _service_versioning
