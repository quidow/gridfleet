import sys
from typing import TYPE_CHECKING

from app.webhooks import dispatcher as _dispatcher

if TYPE_CHECKING:
    from app.webhooks.dispatcher import *  # noqa: F403

sys.modules[__name__] = _dispatcher
