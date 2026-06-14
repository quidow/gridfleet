from app.devices.services.identity import derive_pack_identity


def test_derive_pack_identity_uses_manifest_scheme_when_value_supplied() -> None:
    result = derive_pack_identity(
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="serial-1",
        connection_target="serial-1",
        ip_address=None,
    )

    assert result == ("android_serial", "host", "serial-1", "serial-1", None)


def test_derive_pack_identity_uses_network_target_when_identity_missing() -> None:
    result = derive_pack_identity(
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value=None,
        connection_target="192.168.1.44",
        ip_address="192.168.1.44",
    )

    assert result == ("roku_serial", "global", "192.168.1.44", "192.168.1.44", "192.168.1.44")
