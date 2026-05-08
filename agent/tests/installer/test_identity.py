import pwd
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_app.installer.identity import OperatorIdentity, resolve_operator_identity


def test_explicit_login_wins_over_sudo_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "alice")
    fake_pw = pwd.struct_passwd(("bob", "x", 1001, 1001, "Bob", "/home/bob", "/bin/sh"))
    with patch("agent_app.installer.identity.pwd.getpwnam", return_value=fake_pw):
        identity = resolve_operator_identity(login="bob")
    assert identity == OperatorIdentity(login="bob", uid=1001, home=Path("/home/bob"))


def test_sudo_user_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "alice")
    fake_pw = pwd.struct_passwd(("alice", "x", 1002, 1002, "Alice", "/home/alice", "/bin/sh"))
    with patch("agent_app.installer.identity.pwd.getpwnam", return_value=fake_pw):
        identity = resolve_operator_identity(login=None)
    assert identity.login == "alice"
    assert identity.uid == 1002
    assert identity.home == Path("/home/alice")


def test_current_process_when_no_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    fake_pw = pwd.struct_passwd(("ci", "x", 4242, 4242, "CI", "/home/ci", "/bin/sh"))
    with (
        patch("agent_app.installer.identity.getpass.getuser", return_value="ci"),
        patch("agent_app.installer.identity.pwd.getpwnam", return_value=fake_pw),
    ):
        identity = resolve_operator_identity(login=None)
    assert identity == OperatorIdentity(login="ci", uid=4242, home=Path("/home/ci"))


def test_unknown_login_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    with (
        patch("agent_app.installer.identity.pwd.getpwnam", side_effect=KeyError("nope")),
        pytest.raises(ValueError, match="unknown login"),
    ):
        resolve_operator_identity(login="nobody")
