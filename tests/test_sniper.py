from backend.sniper import should_bid, next_bid_amount, projected_exposure_total

def test_should_bid_under_cap():
    assert should_bid(current_price=50.0, max_cap=100.0, increment=5.0) is True

def test_should_not_bid_at_cap():
    assert should_bid(current_price=100.0, max_cap=100.0, increment=5.0) is False

def test_should_not_bid_over_cap():
    assert should_bid(current_price=95.0, max_cap=100.0, increment=10.0) is False

def test_should_bid_exact_cap():
    assert should_bid(current_price=95.0, max_cap=100.0, increment=5.0) is True

def test_next_bid_amount():
    assert next_bid_amount(current_price=50.0, increment=5.0) == 55.0

def test_next_bid_amount_large_increment():
    assert next_bid_amount(current_price=200.0, increment=10.0) == 210.0


def test_projected_exposure_total_replaces_existing_commitment():
    # exposure_total includes this snipe at $40; replacing with $55 should add $15
    assert projected_exposure_total(120.0, 40.0, 55.0) == 135.0


def test_projected_exposure_total_with_no_prior_commitment():
    assert projected_exposure_total(120.0, 0.0, 25.0) == 145.0
