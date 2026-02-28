from backend.calculator import calculate_true_cost, get_verdict

HST_RATE = 0.13

def test_basic_true_cost():
    result = calculate_true_cost(bid_price=100.0, premium_pct=15.0)
    assert result["bid_price"] == 100.0
    assert result["premium_amount"] == 15.0
    assert result["subtotal"] == 115.0
    assert result["tax_amount"] == 14.95
    assert result["total"] == 129.95

def test_true_cost_different_premium():
    result = calculate_true_cost(bid_price=200.0, premium_pct=16.0)
    assert result["premium_amount"] == 32.0
    assert result["subtotal"] == 232.0
    assert result["total"] == 262.16

def test_true_cost_zero_bid():
    result = calculate_true_cost(bid_price=0.0, premium_pct=15.0)
    assert result["total"] == 0.0

def test_verdict_good_deal():
    verdict = get_verdict(true_cost=80.0, ebay_avg_sold=120.0)
    assert verdict == "good_deal"

def test_verdict_fair():
    verdict = get_verdict(true_cost=110.0, ebay_avg_sold=120.0)
    assert verdict == "fair"

def test_verdict_overpriced():
    verdict = get_verdict(true_cost=140.0, ebay_avg_sold=120.0)
    assert verdict == "overpriced"

def test_verdict_no_ebay_data():
    verdict = get_verdict(true_cost=100.0, ebay_avg_sold=None)
    assert verdict == "unknown"
