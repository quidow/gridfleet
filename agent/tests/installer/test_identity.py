from __future__ import annotations

import getpass
import pwd
from pathlib import Path

import pytest

from agent_app.installer.identity import OperatorIdentity, resolve_operator_identity


def test_resolve_operator_identity_uses_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "rootescalated")
    current = getpass.getuser()
    record = pwd.getpwnam(current)

    operator = resolve_operator_identity()

    assert operator == OperatorIdentity(login=current, uid=record.pw_uid, home=Path(record.pw_dir))


def test_resolve_operator_identity_accepts_explicit_login() -> None:
    current = getpass.getuser()
    record = pwd.getpwnam(current)

    operator = resolve_operator_identity(login=current)

    assert operator.login == current
    assert operator.uid == record.pw_uid


def test_resolve_operator_identity_rejects_unknown_login() -> None:
    with pytest.raises(ValueError, match="unknown login"):
        resolve_operator_identity(login="definitely-not-a-real-user-xyzzy")
