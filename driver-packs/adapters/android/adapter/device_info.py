"""Android device identity helpers."""

from __future__ import annotations

_FIRE_TV_MODEL_NAMES: dict[str, str] = {
    "AFTB": "Fire TV (1st Gen)",
    "AFTS": "Fire TV (2nd Gen)",
    "AFTN": "Fire TV (3rd Gen)",
    "AFTM": "Fire TV Stick (1st Gen)",
    "AFTT": "Fire TV Stick (2nd Gen)",
    "AFTSSS": "Fire TV Stick (3rd Gen)",
    "AFTSS": "Fire TV Stick Lite (3rd Gen)",
    "AFTMM": "Fire TV Stick 4K (1st Gen)",
    "AFTKM": "Fire TV Stick 4K (2nd Gen)",
    "AFTKA": "Fire TV Stick 4K Max (1st Gen)",
    "AFTKRT": "Fire TV Stick 4K Max (2nd Gen)",
    "AFTMA08C15": "Fire TV Stick 4K Plus",
    "AFTA": "Fire TV Cube (1st Gen)",
    "AFTR": "Fire TV Cube (2nd Gen)",
    "AFTGAZL": "Fire TV Cube (3rd Gen)",
    "B0CQN8PP9G": "Fire TV Stick HD",
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
    return props.get("product_model", "")


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
