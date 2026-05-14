import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.events.event_bus import *  # noqa: F403

sys.modules[__name__] = import_module("app.events.event_bus")
