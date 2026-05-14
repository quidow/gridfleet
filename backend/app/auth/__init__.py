"""Auth domain package.

Phase 1 of the backend domain-layout refactor introduces this package.
Until later phases land, several auth submodules still hold their old
locations under ``app/services/`` / ``app/routers/`` / ``app/schemas/``
as legacy shims. The canonical implementations live here.
"""

from app.auth.config import AuthConfig

auth_settings = AuthConfig()
