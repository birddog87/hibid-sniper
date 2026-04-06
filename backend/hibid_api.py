"""Direct HiBid GraphQL API calls — no Playwright needed."""
import asyncio
import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "hibid_cookies.json")
GRAPHQL_URL = "https://hibid.com/graphql"

LOTBID_QUERY = """mutation LotBid($lotId: Int!, $bidAmount: Decimal!, $reConfirmed: Boolean!) {
    bid(input: {lotId: $lotId, bidAmount: $bidAmount, reConfirmed: $reConfirmed}) {
        __typename
        ... on BidResultType { bidStatus suggestedBid bidMessage }
        ... on InvalidInputError { messages errors { fieldName messages } }
    }
}"""

REGISTER_QUERY = """mutation RegisterBuyer($acceptTermsAndConditions: Boolean!, $auctionId: Int!, $buyerPayInfoId: Int, $notes: String, $isShippingRequested: Boolean!) {
    registerBuyer(input: {acceptTermsAndConditions: $acceptTermsAndConditions, auctionId: $auctionId, buyerPayInfoId: $buyerPayInfoId, notes: $notes, isShippingRequested: $isShippingRequested}) {
        __typename
        ... on BuyerRegistrationType { body subject }
        ... on InvalidInputError { messages }
    }
}"""

PAYINFO_QUERY = """query BuyerPayInfo { buyerPayInfo { ... on BuyerPayInfo { id } } }"""


def _load_cookies() -> list[dict]:
    if not os.path.exists(COOKIE_FILE):
        return []
    with open(COOKIE_FILE) as f:
        return json.load(f)


def get_auth_token() -> str | None:
    """Read the sessionId JWT from the saved cookies file."""
    for c in _load_cookies():
        if c.get("name") == "sessionId":
            return c["value"]
    return None


def _build_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Origin": "https://hibid.com",
        "Referer": "https://hibid.com/",
    }


def _build_cookies() -> dict:
    cookies = {}
    for c in _load_cookies():
        if c.get("name") == "__cf_bm":
            cookies["__cf_bm"] = c["value"]
    return cookies


MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # seconds


async def _graphql_post(client: httpx.AsyncClient, headers: dict, cookies: dict, payload: dict) -> httpx.Response:
    """POST to GraphQL endpoint with retry + exponential backoff on transient failures."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(GRAPHQL_URL, headers=headers, json=payload, cookies=cookies)
            if resp.status_code < 500:
                return resp
            logger.warning(f"GraphQL HTTP {resp.status_code}, retry {attempt + 1}/{MAX_RETRIES}")
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            last_exc = e
            logger.warning(f"GraphQL request failed ({e}), retry {attempt + 1}/{MAX_RETRIES}")
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_BACKOFF[attempt])
    if last_exc:
        raise last_exc
    return resp  # return the 5xx response so caller can handle


async def _get_auction_id(lot_url: str) -> int | None:
    """Fetch the lot page HTML and extract the auction ID from the Apollo cache."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(lot_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            })
        match = re.search(r'"Auction:(\d+)"', resp.text)
        if match:
            return int(match.group(1))
    except Exception as e:
        logger.error(f"Failed to get auction ID from {lot_url}: {e}")
    return None


async def _get_payment_method_id(token: str) -> int | None:
    """Fetch the first payment method ID on file."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await _graphql_post(client,
                headers=_build_headers(token),
                cookies=_build_cookies(),
                payload={"query": PAYINFO_QUERY})
        pay_list = resp.json().get("data", {}).get("buyerPayInfo", [])
        if pay_list:
            return pay_list[0]["id"]
    except Exception as e:
        logger.error(f"Failed to get payment method: {e}")
    return None


async def _register_for_auction(token: str, auction_id: int) -> bool:
    """Auto-register as a bidder for an auction."""
    pay_id = await _get_payment_method_id(token)
    logger.info(f"Registering for auction {auction_id} (payment method: {pay_id})")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await _graphql_post(client,
                headers=_build_headers(token),
                cookies=_build_cookies(),
                payload={
                    "operationName": "RegisterBuyer",
                    "variables": {
                        "acceptTermsAndConditions": True,
                        "auctionId": auction_id,
                        "buyerPayInfoId": pay_id,
                        "notes": None,
                        "isShippingRequested": False,
                    },
                    "query": REGISTER_QUERY,
                })
        reg = resp.json().get("data", {}).get("registerBuyer", {})
        if reg.get("__typename") == "BuyerRegistrationType":
            logger.info("Auto-registered for auction")
            return True
        else:
            logger.warning(f"Registration result: {reg}")
            return True  # Often already registered, still ok
    except Exception as e:
        logger.error(f"Registration failed: {e}")
        return False


async def place_bid_direct(lot_id: int, bid_amount: float, lot_url: str | None = None) -> dict:
    """Place a bid via HiBid's GraphQL API using httpx.

    If we get RegisterFirst, auto-registers and retries once.
    Returns: {success, status, message, suggested_bid}
    """
    token = get_auth_token()
    if not token:
        return {"success": False, "status": "error", "message": "No auth token — import cookies first", "suggested_bid": None}

    headers = _build_headers(token)
    cookies = _build_cookies()
    payload = {
        "operationName": "LotBid",
        "variables": {"lotId": lot_id, "bidAmount": bid_amount, "reConfirmed": True},
        "query": LOTBID_QUERY,
    }

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await _graphql_post(client, headers=headers, cookies=cookies, payload=payload)

            if resp.status_code != 200:
                logger.error(f"GraphQL HTTP {resp.status_code}: {resp.text[:200]}")
                return {"success": False, "status": "error", "message": f"HTTP {resp.status_code}", "suggested_bid": None}

            data = resp.json()
            bid_data = data.get("data", {}).get("bid", {})
            typename = bid_data.get("__typename", "")

            if typename == "BidResultType":
                status = bid_data.get("bidStatus", "")
                message = bid_data.get("bidMessage", "")
                suggested = bid_data.get("suggestedBid")
                logger.info(f"Bid result: status={status}, message={message}")

                # Auto-register and retry on RegisterFirst
                if status == "NO_BID" and message == "RegisterFirst" and attempt == 0 and lot_url:
                    logger.info("Not registered — attempting auto-registration...")
                    auction_id = await _get_auction_id(lot_url)
                    if auction_id:
                        await _register_for_auction(token, auction_id)
                        continue  # retry the bid
                    else:
                        return {"success": False, "status": "NO_BID", "message": "RegisterFirst — could not find auction ID", "suggested_bid": None}

                return {
                    "success": status in ("WINNING", "OUTBID", "ACCEPTED"),
                    "status": status,
                    "message": message or "",
                    "suggested_bid": float(suggested) if suggested else None,
                }
            elif typename == "InvalidInputError":
                messages = bid_data.get("messages", [])
                logger.error(f"Bid rejected: {messages}")
                return {"success": False, "status": "rejected", "message": "; ".join(messages), "suggested_bid": None}
            else:
                logger.error(f"Unexpected GraphQL response: {data}")
                return {"success": False, "status": "error", "message": str(data)[:200], "suggested_bid": None}

        except Exception as e:
            logger.error(f"place_bid_direct error: {e}")
            return {"success": False, "status": "error", "message": str(e), "suggested_bid": None}

    return {"success": False, "status": "error", "message": "Failed after retry", "suggested_bid": None}


async def get_lot_status_via_html(lot_url: str) -> dict | None:
    """Fetch lot page HTML and extract live status from Apollo SSR data.
    No Playwright needed — immune to frozen page timers."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(lot_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
        html = resp.text

        # Extract timeLeftSeconds
        secs_match = re.search(r'"timeLeftSeconds"\s*:\s*([\d.]+)', html)
        seconds_left = float(secs_match.group(1)) if secs_match else None

        # Extract current bid from SSR: "currentBidAmount":27.5
        bid_match = re.search(r'"currentBidAmount"\s*:\s*([\d.]+)', html)
        current_bid = float(bid_match.group(1)) if bid_match else None

        # Fallback: parse from visible text "High Bid: 27.50 CAD"
        if current_bid is None:
            vis_match = re.search(r'High Bid:\s*([\d,]+\.?\d*)', html)
            if vis_match:
                current_bid = float(vis_match.group(1).replace(',', ''))

        # Extract bid count
        count_match = re.search(r'"bidCount"\s*:\s*(\d+)', html)
        bid_count = int(count_match.group(1)) if count_match else None

        # Check if ended
        ended = bool(re.search(r'Bidding\s+(Closed|has Closed)', html, re.IGNORECASE))

        # Extract lot-specific timeLeftTitle to verify this is our lot's data
        title_match = re.search(r'"timeLeftTitle"\s*:\s*"([^"]*)"', html)
        time_title = title_match.group(1) if title_match else None

        return {
            "current_price": current_bid,
            "seconds_left": seconds_left,
            "is_ended": ended,
            "bid_count": bid_count,
            "time_title": time_title,
        }
    except Exception as e:
        logger.warning(f"get_lot_status_via_html failed: {e}")
        return None
