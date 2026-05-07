from __future__ import annotations

from app.models.device_group import DeviceGroup, GroupType
from app.models.setting import Setting
from app.seeding.factories.meta import make_device_group, make_setting
from tests.seeding.helpers import build_test_seed_context


def test_make_device_group_static_defaults() -> None:
    ctx = build_test_seed_context(seed=1)
    group = make_device_group(ctx, name="lab-phones")
    assert isinstance(group, DeviceGroup)
    assert group.name == "lab-phones"
    assert group.group_type is GroupType.static
    assert group.description is None
    assert group.filters is None


def test_make_device_group_dynamic_with_filters() -> None:
    ctx = build_test_seed_context(seed=3)
    group = make_device_group(
        ctx,
        name="android-fleet",
        group_type=GroupType.dynamic,
        description="All Android devices",
        filters={"platform_id": "android_mobile"},
    )
    assert group.group_type is GroupType.dynamic
    assert group.description == "All Android devices"
    assert group.filters == {"platform_id": "android_mobile"}


def test_make_setting_sets_key_fields() -> None:
    ctx = build_test_seed_context(seed=1)
    setting = make_setting(ctx, key="node_start_timeout", value=60, category="nodes")
    assert isinstance(setting, Setting)
    assert setting.key == "node_start_timeout"
    assert setting.value == 60
    assert setting.category == "nodes"


def test_make_setting_null_value_allowed() -> None:
    ctx = build_test_seed_context(seed=2)
    setting = make_setting(ctx, key="optional_feature", value=None, category="features")
    assert setting.value is None
    assert setting.key == "optional_feature"
