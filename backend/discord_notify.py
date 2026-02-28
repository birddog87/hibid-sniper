import os
import httpx

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


def _embed(title: str, color: int, fields: list, url: str = "") -> dict:
    return {
        "embeds": [{
            "title": title,
            "url": url,
            "color": color,
            "fields": fields,
        }]
    }


def format_snipe_won(lot_title: str, lot_url: str, winning_bid: float, true_cost: float) -> dict:
    return _embed(
        title=f"Won: {lot_title}",
        color=0x00FF00,
        url=lot_url,
        fields=[
            {"name": "Winning Bid", "value": f"${winning_bid:.2f}", "inline": True},
            {"name": "True Cost", "value": f"${true_cost:.2f}", "inline": True},
        ],
    )


def format_snipe_lost(lot_title: str, lot_url: str, final_price: float, your_cap: float) -> dict:
    return _embed(
        title=f"Lost: {lot_title}",
        color=0xFF0000,
        url=lot_url,
        fields=[
            {"name": "Final Price", "value": f"${final_price:.2f}", "inline": True},
            {"name": "Your Cap", "value": f"${your_cap:.2f}", "inline": True},
        ],
    )


def format_snipe_capped(lot_title: str, lot_url: str, current_price: float, your_cap: float) -> dict:
    return _embed(
        title=f"Capped Out: {lot_title}",
        color=0xFFA500,
        url=lot_url,
        fields=[
            {"name": "Current Price", "value": f"${current_price:.2f}", "inline": True},
            {"name": "Your Cap", "value": f"${your_cap:.2f}", "inline": True},
        ],
    )


async def send_notification(payload: dict):
    if not DISCORD_WEBHOOK_URL:
        return
    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10.0)
