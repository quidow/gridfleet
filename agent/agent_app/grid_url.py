from __future__ import annotations

import socket
from typing import cast

from agent_app.config import agent_settings


def get_local_ip() -> str:
    """Detect the local IP address reachable by the manager."""
    if agent_settings.advertise_ip:
        return agent_settings.advertise_ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            sockname = cast("tuple[str, int]", s.getsockname())
            return sockname[0]
        finally:
            s.close()
    except OSError:
        return socket.gethostbyname(socket.gethostname())
