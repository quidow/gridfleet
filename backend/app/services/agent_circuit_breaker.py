import sys
from typing import TYPE_CHECKING

from app.agent_comm import circuit_breaker as _circuit_breaker

if TYPE_CHECKING:
    from app.agent_comm.circuit_breaker import *  # noqa: F403

sys.modules[__name__] = _circuit_breaker
