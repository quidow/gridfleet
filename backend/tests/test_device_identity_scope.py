from __future__ import annotations

from app.services.device_identity import is_host_scoped_identity


def test_host_scoped() -> None:
    assert is_host_scoped_identity(identity_scope="host") is True


def test_global_scoped() -> None:
    assert is_host_scoped_identity(identity_scope="global") is False


def test_none_scoped() -> None:
    assert is_host_scoped_identity(identity_scope=None) is False
