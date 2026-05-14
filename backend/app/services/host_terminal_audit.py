import sys
from typing import TYPE_CHECKING

from app.hosts import service_terminal_audit as _service_terminal_audit

if TYPE_CHECKING:
    from app.hosts.service_terminal_audit import *  # noqa: F403

sys.modules[__name__] = _service_terminal_audit
