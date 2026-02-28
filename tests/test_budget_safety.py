import pytest
from fastapi import HTTPException

from backend.main import _validate_bid_safety


def _budget(**overrides):
    base = {
        "global_spend_cap": 500.0,
        "max_single_snipe_cap": 200.0,
        "remaining": 300.0,
        "exposure_total": 250.0,
        "emergency_bid_hard_max": 0.0,
    }
    base.update(overrides)
    return base


def test_validate_bid_safety_allows_safe_bid():
    _validate_bid_safety(
        budget=_budget(),
        bid_amount=120.0,
        snipe_cap=150.0,
        previous_commitment=110.0,
    )


def test_validate_bid_safety_blocks_when_cap_unset():
    with pytest.raises(HTTPException):
        _validate_bid_safety(
            budget=_budget(global_spend_cap=0.0),
            bid_amount=50.0,
            snipe_cap=100.0,
            previous_commitment=20.0,
        )


def test_validate_bid_safety_blocks_bid_above_snipe_cap():
    with pytest.raises(HTTPException):
        _validate_bid_safety(
            budget=_budget(),
            bid_amount=151.0,
            snipe_cap=150.0,
            previous_commitment=100.0,
        )


def test_validate_bid_safety_blocks_projected_exposure():
    with pytest.raises(HTTPException):
        _validate_bid_safety(
            budget=_budget(global_spend_cap=300.0, exposure_total=280.0),
            bid_amount=100.0,
            snipe_cap=150.0,
            previous_commitment=20.0,
        )


def test_validate_bid_safety_blocks_emergency_hard_max():
    with pytest.raises(HTTPException):
        _validate_bid_safety(
            budget=_budget(emergency_bid_hard_max=75.0),
            bid_amount=80.0,
            snipe_cap=150.0,
            previous_commitment=30.0,
        )
