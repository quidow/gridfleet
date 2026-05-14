import sys
from typing import TYPE_CHECKING

from app.hosts import service_terminal_proxy as _service_terminal_proxy

if TYPE_CHECKING:
    from app.hosts.service_terminal_proxy import *  # noqa: F403

sys.modules[__name__] = _service_terminal_proxy
