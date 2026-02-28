from backend.discord_notify import format_snipe_won, format_snipe_lost, format_snipe_capped

def test_format_snipe_won():
    msg = format_snipe_won(
        lot_title="Milwaukee M18 Drill",
        lot_url="https://hibid.com/lot/123/test",
        winning_bid=55.0,
        true_cost=71.93,
    )
    assert "Milwaukee M18 Drill" in msg["embeds"][0]["title"]
    assert "55.0" in str(msg["embeds"][0]["fields"])
    assert msg["embeds"][0]["color"] == 0x00FF00

def test_format_snipe_lost():
    msg = format_snipe_lost(
        lot_title="Some Item",
        lot_url="https://hibid.com/lot/456/test",
        final_price=120.0,
        your_cap=100.0,
    )
    assert msg["embeds"][0]["color"] == 0xFF0000

def test_format_snipe_capped():
    msg = format_snipe_capped(
        lot_title="Expensive Thing",
        lot_url="https://hibid.com/lot/789/test",
        current_price=105.0,
        your_cap=100.0,
    )
    assert "Capped Out" in msg["embeds"][0]["title"]
