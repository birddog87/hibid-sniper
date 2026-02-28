import re
import logging
import random
import urllib.parse
import asyncio
import httpx
from dataclasses import dataclass

logger = logging.getLogger(__name__)

STARTPAGE_URL = "https://www.startpage.com/sp/search"
DDG_URL = "https://html.duckduckgo.com/html/"

# Rotate user agents to look less like a bot
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# Junk listing keywords - titles containing these are filtered out
JUNK_KEYWORDS = re.compile(
    r'\b('
    r'for\s+parts|parts\s+only|broken|defective|damaged|faulty|'
    r'not\s+working|as[\s-]is|untested|salvage|repair|'
    r'case\s+only|case\s+for|cover\s+for|skin\s+for|'
    r'replacement\s+part|replacement\s+battery|'
    r'charging\s+cable|usb\s+cable|cable\s+only|dongle\s+only|'
    r'mouse\s+pad|mousepad|wrist\s+rest|carrying\s+case|'
    r'sticker|decal|template|manual\s+only'
    r')\b',
    re.IGNORECASE,
)


@dataclass
class EbayListing:
    title: str
    price: float
    url: str
    sold: bool = False


def _random_headers(referer: str | None = None) -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": random.choice([
            "en-CA,en-US;q=0.9,en;q=0.8",
            "en-US,en;q=0.9",
            "en-GB,en;q=0.9,en-US;q=0.8",
        ]),
        "Accept-Encoding": "gzip, deflate, br",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def build_ebay_search_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.ebay.ca/sch/i.html?_nkw={encoded}&_sop=15"


def build_ebay_sold_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.ebay.ca/sch/i.html?_nkw={encoded}&LH_Complete=1&LH_Sold=1&_sop=15"


def build_amazon_search_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.amazon.ca/s?k={encoded}"


def build_kijiji_search_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.kijiji.ca/b-ontario/k0l9004?q={encoded}"


def build_fb_marketplace_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.facebook.com/marketplace/burlington/search?query={encoded}"


def parse_price(price_str: str | None) -> float | None:
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", price_str.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _empty_result(search_url: str) -> dict:
    return {
        "count": 0,
        "low": None,
        "high": None,
        "avg": None,
        "listings": [],
        "search_url": search_url,
    }


def _build_result(listings: list[dict], search_url: str) -> dict:
    if not listings:
        return _empty_result(search_url)
    prices = [l["price"] for l in listings]
    return {
        "count": len(prices),
        "low": min(prices),
        "high": max(prices),
        "avg": round(sum(prices) / len(prices), 2),
        "listings": listings[:10],
        "search_url": search_url,
    }


# ---------------------------------------------------------------------------
# Startpage (primary - better results)
# ---------------------------------------------------------------------------

async def _startpage_search(client: httpx.AsyncClient, query: str) -> str | None:
    """Search via Startpage. Returns HTML or None if blocked/failed."""
    try:
        # Small random delay to avoid looking like a bot
        await asyncio.sleep(random.uniform(0.3, 1.5))
        resp = await client.get(
            STARTPAGE_URL,
            params={"query": query},
            headers=_random_headers(referer="https://www.startpage.com/"),
            follow_redirects=True,
            timeout=15.0,
        )
        if resp.status_code == 200:
            # Check for CAPTCHA block
            if "captcha" in resp.text.lower()[:2000]:
                logger.warning("Startpage returned CAPTCHA - blocked")
                return None
            return resp.text
        logger.warning(f"Startpage returned {resp.status_code} for: {query}")
    except Exception as e:
        logger.warning(f"Startpage search failed for '{query}': {e}")
    return None


def _parse_startpage_prices(html: str) -> list[dict]:
    """Extract prices and titles from Startpage search results."""
    results = []

    title_links = re.finditer(
        r'<a[^>]*class="[^"]*result-title[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )

    for m in title_links:
        url = m.group(1).replace("&amp;", "&")
        if "ebay.ca" not in url and "ebay.com" not in url:
            continue

        raw_title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        title = re.sub(r"\.css-[^{]+\{[^}]*\}", "", raw_title)
        title = re.sub(r"@media\s*\([^)]*\)\s*\{[^}]*\}", "", title).strip()
        if not title:
            continue

        after_title = html[m.end():m.end() + 2000]
        desc_match = re.search(
            r'<p[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</p>',
            after_title,
            re.DOTALL,
        )
        desc_text = ""
        if desc_match:
            desc_text = re.sub(r"<[^>]+>", "", desc_match.group(1))

        text = f"{title} {desc_text}"

        if JUNK_KEYWORDS.search(text):
            continue

        price_matches = re.findall(
            r'(?:C\s*\$|CA\$|CAD\s*\$?|\$)\s*([\d,]+\.?\d*)',
            text,
        )
        for pm in price_matches:
            price = parse_price(pm)
            if price and 1.0 < price < 50000:
                results.append({"title": title, "price": price, "url": url})
                break

    return results


# ---------------------------------------------------------------------------
# DuckDuckGo (fallback - always works but fewer prices)
# ---------------------------------------------------------------------------

async def _ddg_search(client: httpx.AsyncClient, query: str) -> str | None:
    """Search via DuckDuckGo HTML. Reliable fallback."""
    try:
        await asyncio.sleep(random.uniform(1.5, 3.5))
        # Try POST first, fall back to GET
        resp = await client.post(
            DDG_URL,
            data={"q": query},
            headers=_random_headers(),
            follow_redirects=True,
            timeout=15.0,
        )
        if resp.status_code != 200:
            # Retry with GET
            resp = await client.get(
                DDG_URL,
                params={"q": query},
                headers=_random_headers(),
                follow_redirects=True,
                timeout=15.0,
            )
        if resp.status_code == 200:
            # DDG sometimes returns a tiny empty page when throttling
            if len(resp.text) < 8000:
                logger.warning(f"DDG throttled (tiny response {len(resp.text)} chars) for: {query[:40]}")
                return None
            return resp.text
        logger.warning(f"DuckDuckGo returned {resp.status_code} for: {query}")
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed for '{query}': {e}")
    return None


def _parse_ddg_prices(html: str) -> list[dict]:
    """Extract prices and titles from DuckDuckGo HTML search results."""
    results = []

    title_links = re.finditer(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )

    for m in title_links:
        raw_url = m.group(1).replace("&amp;", "&")
        url_match = re.search(r"uddg=([^&]+)", raw_url)
        url = urllib.parse.unquote(url_match.group(1)) if url_match else raw_url

        if "ebay.ca" not in url and "ebay.com" not in url:
            continue

        raw_title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not raw_title:
            continue

        after_title = html[m.end() : m.end() + 2000]
        snip_match = re.search(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|td)',
            after_title,
            re.DOTALL,
        )
        snippet = ""
        if snip_match:
            snippet = re.sub(r"<[^>]+>", "", snip_match.group(1))

        text = f"{raw_title} {snippet}"

        if JUNK_KEYWORDS.search(text):
            continue

        price_matches = re.findall(
            r"(?:C\s*\$|CA\$|CAD\s*\$?|\$)\s*([\d,]+\.?\d*)",
            text,
        )
        for pm in price_matches:
            price = parse_price(pm)
            if price and 1.0 < price < 50000:
                results.append({"title": raw_title, "price": price, "url": url})
                break

    return results


# ---------------------------------------------------------------------------
# Main search: Startpage first, DDG fallback
# ---------------------------------------------------------------------------

async def search_ebay_via_startpage(query: str) -> dict:
    """Search for eBay prices. Tries Startpage first (better results),
    falls back to DuckDuckGo if Startpage is blocked/CAPTCHAd.
    """
    active_url = build_ebay_search_url(query)
    sold_url = build_ebay_sold_url(query)

    listings1 = []
    listings2 = []
    source = "none"

    try:
        async with httpx.AsyncClient() as client:
            # Try Startpage first
            q1 = f"ebay.ca {query} price"
            q2 = f"{query} ebay sold price"
            html1 = await _startpage_search(client, q1)
            html2 = await _startpage_search(client, q2) if html1 else None

            if html1:
                listings1 = _parse_startpage_prices(html1)
                listings2 = _parse_startpage_prices(html2) if html2 else []
                source = "startpage"
            else:
                # Fall back to DDG
                logger.warning("Startpage blocked, falling back to DuckDuckGo")
                dq1 = f"ebay.ca {query} price CAD"
                dq2 = f"ebay.ca {query} sold price CAD"
                dhtml1 = await _ddg_search(client, dq1)
                dhtml2 = await _ddg_search(client, dq2)
                listings1 = _parse_ddg_prices(dhtml1) if dhtml1 else []
                listings2 = _parse_ddg_prices(dhtml2) if dhtml2 else []
                source = "duckduckgo"
    except Exception as e:
        logger.error(f"Search failed entirely: {e}")

    # Deduplicate each set by URL
    seen_active = set()
    active_deduped = []
    for l in listings1:
        if l["url"] not in seen_active:
            seen_active.add(l["url"])
            active_deduped.append(l)

    seen_sold = set()
    sold_deduped = []
    for l in listings2:
        if l["url"] not in seen_sold and l["url"] not in seen_active:
            seen_sold.add(l["url"])
            sold_deduped.append(l)

    if source == "duckduckgo":
        logger.info(f"DDG raw listings: q1={len(listings1)}, q2={len(listings2)}")
    logger.info(f"eBay search via {source}: {len(active_deduped)} active, {len(sold_deduped)} sold for '{query}'")

    active_result = _build_result(active_deduped, active_url)
    sold_result = _build_result(sold_deduped, sold_url)

    return {
        "active": active_result,
        "sold": sold_result,
        "amazon_url": build_amazon_search_url(query),
        "kijiji_url": build_kijiji_search_url(query),
        "fb_marketplace_url": build_fb_marketplace_url(query),
    }


# Keep old function name as alias
async def search_ebay(query: str) -> dict:
    return await search_ebay_via_startpage(query)
