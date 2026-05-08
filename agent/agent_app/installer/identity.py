from __future__ import annotations

import getpass
import os
import pwd
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OperatorIdentity:
    login: str
    uid: int
    home: Path


def resolve_operator_identity(login: str | None = None) -> OperatorIdentity:
    target_login = login or os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        record = pwd.getpwnam(target_login)
    except KeyError as exc:
        raise ValueError(f"unknown login: {target_login!r}") from exc
    return OperatorIdentity(login=record.pw_name, uid=record.pw_uid, home=Path(record.pw_dir))
