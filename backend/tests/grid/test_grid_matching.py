"""W3C capability merge + identity matching — the fiddliest W3C surface, kept exhaustive."""

from typing import Any, ClassVar

import pytest

from app.grid.matching import (
    CapabilityMergeError,
    candidate_matches_stereotype,
    merge_candidates,
    requested_group_keys,
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
        "gridfleet:deviceId": "11111111-1111-1111-1111-111111111111",
        "gridfleet:group:east-lab": True,
        "gridfleet:group:ci": True,
        "appium:platform": "android_tv",
    }

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ({}, True),
            ({"platformName": "android"}, True),  # case-insensitive
            ({"platformName": "iOS"}, False),
            ({"appium:udid": "emulator-5554"}, True),
            ({"appium:udid": "other"}, False),
            ({"gridfleet:deviceId": "11111111-1111-1111-1111-111111111111"}, True),
            ({"gridfleet:deviceId": "22222222-2222-2222-2222-222222222222"}, False),
            # AND semantics: a single candidate requesting both groups matches only
            # a stereotype that advertises both.
            ({"gridfleet:group:east-lab": True, "gridfleet:group:ci": True}, True),
            ({"gridfleet:group:east-lab": True}, True),
            ({"gridfleet:group:ci": True}, True),
            ({"gridfleet:group:west-lab": True}, False),  # not in stereotype
            (
                {"gridfleet:group:east-lab": True, "gridfleet:group:west-lab": True},
                False,
            ),  # one missing -> no match
            ({"appium:newCommandTimeout": 120}, True),  # non-identity appium caps are Appium's problem
            ({"gridfleet:somethingCustom": "x"}, True),  # unknown vendor keys do not constrain slot identity
            ({"appium:deviceName": "whatever"}, False),  # identity key absent from stereotype -> no match
            ({"appium:platform": "android_tv"}, True),  # matching pack platform_id
            ({"appium:platform": "firetv_real"}, False),  # different pack platform_id -> no match
        ],
    )
    def test_matrix(self, candidate: dict[str, Any], expected: bool) -> None:
        assert candidate_matches_stereotype(candidate, self.STEREO) is expected

    def test_first_match_candidates_remain_alternatives(self) -> None:
        """A request with two firstMatch candidates, each pinning a different group,
        matches a stereotype that advertises either group. firstMatch is OR."""
        body = {
            "capabilities": {
                "firstMatch": [
                    {"gridfleet:group:east-lab": True},
                    {"gridfleet:group:west-lab": True},
                ]
            }
        }
        candidates = merge_candidates(body)
        east_only = {"gridfleet:group:east-lab": True}
        west_only = {"gridfleet:group:west-lab": True}
        assert candidate_matches_stereotype(candidates[0], east_only) is True
        assert candidate_matches_stereotype(candidates[1], west_only) is True
        assert candidate_matches_stereotype(candidates[0], west_only) is False
        assert candidate_matches_stereotype(candidates[1], east_only) is False


class TestRequestedGroupKeys:
    @pytest.mark.parametrize(
        "value",
        [False, "true", 1, None],
    )
    def test_group_capability_requires_json_true(self, value: object) -> None:
        candidates = [{"gridfleet:group:east-lab": value}]
        with pytest.raises(CapabilityMergeError, match="boolean true"):
            requested_group_keys(candidates)

    def test_legacy_tag_capability_fails_loudly(self) -> None:
        candidates = [{"gridfleet:tag:lab": "east"}]
        with pytest.raises(CapabilityMergeError, match="gridfleet:group:<key>"):
            requested_group_keys(candidates)

    def test_invalid_group_key_is_rejected(self) -> None:
        with pytest.raises(CapabilityMergeError, match="invalid device group key"):
            requested_group_keys([{"gridfleet:group:Bad Key": True}])

    def test_returns_only_group_keys(self) -> None:
        candidates = [
            {"platformName": "Android", "gridfleet:group:east-lab": True},
            {"gridfleet:group:ci": True, "appium:udid": "emulator-5554"},
        ]
        assert requested_group_keys(candidates) == frozenset({"east-lab", "ci"})

    def test_and_within_one_candidate(self) -> None:
        candidates = [{"gridfleet:group:east-lab": True, "gridfleet:group:ci": True}]
        assert requested_group_keys(candidates) == frozenset({"east-lab", "ci"})

    def test_or_across_first_match_candidates(self) -> None:
        candidates = [
            {"gridfleet:group:east-lab": True},
            {"gridfleet:group:west-lab": True},
        ]
        # Both keys are surfaced; the matcher's OR happens per-candidate.
        assert requested_group_keys(candidates) == frozenset({"east-lab", "west-lab"})

    def test_no_group_keys_returns_empty(self) -> None:
        assert requested_group_keys([{"platformName": "Android"}]) == frozenset()
