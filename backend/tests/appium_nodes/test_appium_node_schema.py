"""Pure ORM schema guards for AppiumNode."""

from sqlalchemy import String

from app.appium_nodes.models import AppiumNode


def test_appium_node_observed_pack_release_is_nullable_string() -> None:
    column = AppiumNode.__table__.c.observed_pack_release

    assert isinstance(column.type, String)
    assert column.nullable is True
