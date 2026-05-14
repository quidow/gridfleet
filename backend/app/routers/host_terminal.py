import sys
from typing import TYPE_CHECKING

from app.hosts import router_terminal as _router_terminal

if TYPE_CHECKING:
    from app.hosts.router_terminal import *  # noqa: F403

sys.modules[__name__] = _router_terminal
