"""W3C capability merge + identity matching — the fiddliest W3C surface, kept exhaustive."""

from typing import Any, ClassVar

import pytest

from app.grid.matching import (
    CapabilityMergeError,
    candidate_matches_stereotype,
    merge_candidates,
)


class TestMergeCandidates:
    def test_always_match_only(self) -> None:
        body = {"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
        assert merge_candidates(body) == [{"platformName": "Android"}]

    def test_first_match_fanout_preserves_order(self) -> None:
        body = {
            "capabilities": {
                "alwaysMatch": {"platformName": "Android"},
                "firstMatch": [{"appium:udid": "a"}, {"appium:udid": "b"}],
            }
        }
        assert merge_candidates(body) == [
            {"platformName": "Android", "appium:udid": "a"},
            {"platformName": "Android", "appium:udid": "b"},
        ]

    def test_missing_first_match_means_single_empty_candidate(self) -> None:
        assert merge_candidates({"capabilities": {"alwaysMatch": {}}}) == [{}]

    def test_empty_first_match_list_means_single_empty_candidate(self) -> None:
        assert merge_candidates({"capabilities": {"alwaysMatch": {}, "firstMatch": []}}) == [{}]

    def test_overlapping_key_is_error(self) -> None:
        body = {
            "capabilities": {
                "alwaysMatch": {"platformName": "Android"},
                "firstMatch": [{"platformName": "iOS"}],
            }
        }
        with pytest.raises(CapabilityMergeError):
            merge_candidates(body)

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"capabilities": []},
            {"capabilities": {"alwaysMatch": []}},
            {"capabilities": {"firstMatch": {}}},
            {"capabilities": {"firstMatch": ["nope"]}},
            {"desiredCapabilities": {"platformName": "Android"}},  # MJSONWP-only is rejected
        ],
    )
    def test_invalid_shapes_raise(self, body: dict[str, Any]) -> None:
        with pytest.raises(CapabilityMergeError):
            merge_candidates(body)


class TestCandidateMatchesStereotype:
    STEREO: ClassVar[dict[str, Any]] = {
        "platformName": "Android",
        "appium:udid": "emulator-5554",
        "appium:gridfleet:deviceId": "11111111-1111-1111-1111-111111111111",
        "appium:gridfleet:tag:pool": "ci",
    }

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ({}, True),
            ({"platformName": "android"}, True),  # case-insensitive
            ({"platformName": "iOS"}, False),
            ({"appium:udid": "emulator-5554"}, True),
            ({"appium:udid": "other"}, False),
            ({"appium:gridfleet:deviceId": "11111111-1111-1111-1111-111111111111"}, True),
            ({"appium:gridfleet:deviceId": "22222222-2222-2222-2222-222222222222"}, False),
            ({"appium:gridfleet:tag:pool": "ci"}, True),
            ({"appium:gridfleet:tag:pool": "dev"}, False),
            ({"appium:gridfleet:tag:missing": "x"}, False),  # requested tag absent from stereotype
            ({"appium:newCommandTimeout": 120}, True),  # non-identity appium caps are Appium's problem
            ({"gridfleet:run_id": "some-run"}, True),  # run id does not constrain slot identity
            ({"appium:deviceName": "whatever"}, False),  # identity key absent from stereotype -> no match
        ],
    )
    def test_matrix(self, candidate: dict[str, Any], expected: bool) -> None:
        assert candidate_matches_stereotype(candidate, self.STEREO) is expected
