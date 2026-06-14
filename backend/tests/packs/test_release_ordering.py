from app.packs.services.release_ordering import latest_release, parse_release_key


def test_parse_release_key_calver() -> None:
    assert parse_release_key("2026.04.0") == (2026, 4, 0)


def test_parse_release_key_semver() -> None:
    assert parse_release_key("3.6.0") == (3, 6, 0)


def test_parse_release_key_two_segments() -> None:
    assert parse_release_key("2.0") == (2, 0)


def test_ordering_calver_month_padding() -> None:
    assert parse_release_key("2026.04.0") < parse_release_key("2026.10.0")


def test_ordering_calver_patch_double_digit() -> None:
    assert parse_release_key("2026.04.9") < parse_release_key("2026.04.10")


def test_ordering_semver_major() -> None:
    assert parse_release_key("2.0.0") < parse_release_key("10.0.0")


def test_latest_release_picks_highest() -> None:
    class FakeRelease:
        def __init__(self, release: str) -> None:
            self.release = release

    releases = [FakeRelease("2026.04.0"), FakeRelease("2026.04.10"), FakeRelease("2026.04.9")]
    result = latest_release(releases)
    assert result is not None
    assert result.release == "2026.04.10"


def test_latest_release_empty_returns_none() -> None:
    assert latest_release([]) is None


def test_parse_release_key_non_numeric_fallback() -> None:
    key = parse_release_key("beta-1")
    assert isinstance(key, tuple)
