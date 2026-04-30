"""Android device identity helpers."""

from __future__ import annotations

_FIRE_TV_MODEL_NAMES: dict[str, str] = {
    "AFTMM": "Fire TV Stick 4K",
    "mantis": "Fire TV Stick 4K",
    "FIRETVSTICK2018": "Fire TV Stick 4K",
}


def is_fire_tv(props: dict[str, str]) -> bool:
    return bool(props.get("fireos_version")) or (
        props.get("manufacturer", "").lower() == "amazon"
        and "tv" in props.get("characteristics", "").lower()
    )


def model_number(props: dict[str, str]) -> str:
    return (
        props.get("model_number")
        or props.get("product_model")
        or props.get("product_device")
        or props.get("product_type", "")
    )


def model_name(props: dict[str, str]) -> str:
    explicit_name = props.get("model", "")
    if explicit_name and explicit_name != model_number(props):
        return explicit_name

    if is_fire_tv(props):
        for key in ("product_model", "product_device", "product_name", "netflix_model_group"):
            value = props.get(key, "")
            if value in _FIRE_TV_MODEL_NAMES:
                return _FIRE_TV_MODEL_NAMES[value]

    for key in ("vendor_model", "odm_model", "product_model"):
        value = props.get(key, "")
        if value and value != model_number(props):
            return value
    return ""


def software_versions(props: dict[str, str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    fire_os = props.get("fireos_marketing_version") or props.get("fireos_version_name", "").split(" (", 1)[0]
    fire_os_compat = props.get("fireos_version", "")
    if fire_os:
        versions["fire_os"] = fire_os
        if fire_os_compat and fire_os_compat != fire_os:
            versions["fire_os_compat"] = fire_os_compat
    elif fire_os_compat:
        versions["fire_os"] = fire_os_compat
    for key, value in {
        "android": props.get("android_version", ""),
        "sdk": props.get("sdk_version", ""),
        "build": props.get("build_id", ""),
        "build_number": props.get("build_number", ""),
    }.items():
        if value:
            versions[key] = value
    return versions
