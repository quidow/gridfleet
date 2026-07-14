from __future__ import annotations

import pytest

from app.hosts.service import MIN_ORCHESTRATION_CONTRACT_VERSION, validate_orchestration_contract


def test_min_contract_is_v7() -> None:
    assert MIN_ORCHESTRATION_CONTRACT_VERSION == 7


def test_v6_host_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_orchestration_contract({"orchestration_contract_version": 6}, host_label="h")


def test_v7_host_is_accepted() -> None:
    validate_orchestration_contract({"orchestration_contract_version": 7}, host_label="h")
