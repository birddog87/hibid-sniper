HST_RATE = 0.13


def calculate_true_cost(bid_price: float, premium_pct: float, per_item_fee: float = 0.0) -> dict:
    premium_amount = round(bid_price * (premium_pct / 100), 2)
    fee = round(per_item_fee or 0.0, 2)
    subtotal = round(bid_price + premium_amount + fee, 2)
    tax_amount = round(subtotal * HST_RATE, 2)
    total = round(subtotal + tax_amount, 2)
    return {
        "bid_price": bid_price,
        "premium_pct": premium_pct,
        "premium_amount": premium_amount,
        "per_item_fee": fee,
        "subtotal": subtotal,
        "tax_rate": HST_RATE,
        "tax_amount": tax_amount,
        "total": total,
    }


def get_verdict(true_cost: float, ebay_avg_sold: float | None) -> str:
    if ebay_avg_sold is None or ebay_avg_sold == 0:
        return "unknown"
    ratio = true_cost / ebay_avg_sold
    if ratio <= 0.85:
        return "good_deal"
    elif ratio <= 1.10:
        return "fair"
    else:
        return "overpriced"
