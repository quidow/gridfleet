from datetime import datetime

from pydantic import BaseModel


class AuthLoginRequest(BaseModel):
    username: str
    password: str


class AuthSessionRead(BaseModel):
    enabled: bool
    authenticated: bool
    username: str | None
    csrf_token: str | None
    expires_at: datetime | None
