import json
import logging
import os
import re

logger = logging.getLogger(__name__)
from urllib.parse import urlparse
from dataclasses import dataclass
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "hibid_cookies.json")


@dataclass
class LotDetails:
    lot_id: str
    title: str
    current_bid: float
    increment: float
    premium_pct: float | None
    end_time: str | None
    thumbnail_url: str | None
    auction_house: str | None
    lot_url: str


def parse_lot_id_from_url(url: str) -> str:
    """Extract the numeric lot ID from a HiBid lot URL."""
    path = urlparse(url).path.rstrip("/")
    parts = path.split("/")
    lot_idx = parts.index("lot")
    return parts[lot_idx + 1]


def parse_increment(text: str) -> float:
    """Parse a dollar amount string like '$5.00' into a float."""
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    return float(cleaned) if cleaned else 5.0


def parse_premium_from_text(text: str) -> float | None:
    """Extract a buyer's premium percentage from descriptive text."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if match:
        return float(match.group(1))
    return None


def parse_price_from_text(text: str) -> float:
    """Extract a price from text like 'High Bid: 16.00 CAD' or 'Bid 18.00 CAD'."""
    match = re.search(r"([\d,]+\.?\d*)", text)
    if match:
        return float(match.group(1).replace(",", ""))
    return 0.0


# Browser instance management
_browser: BrowserContext | None = None
PROFILE_DIR = "/home/htpc/hibid-sniper/browser_profile"


def _browser_alive() -> bool:
    """Check if the persistent browser context is still usable."""
    if _browser is None:
        return False
    try:
        # BrowserContext doesn't have is_connected(); check via its parent browser
        return _browser.browser is not None and _browser.browser.is_connected()
    except Exception:
        return False


async def get_browser() -> BrowserContext:
    """Get or create a persistent Chromium browser context."""
    global _browser
    if not _browser_alive():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Load saved cookies if available
        await _load_saved_cookies()
    return _browser


def _convert_cookies(raw_cookies: list[dict]) -> list[dict]:
    """Convert Cookie Editor format to Playwright format."""
    converted = []
    same_site_map = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}
    for c in raw_cookies:
        pc = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
        }
        if c.get("expirationDate"):
            pc["expires"] = c["expirationDate"]
        if c.get("httpOnly"):
            pc["httpOnly"] = True
        if c.get("secure"):
            pc["secure"] = True
        ss = c.get("sameSite", "")
        if ss in same_site_map:
            pc["sameSite"] = same_site_map[ss]
        converted.append(pc)
    return converted


async def _load_saved_cookies():
    """Load saved HiBid cookies into the browser context."""
    if not os.path.exists(COOKIE_FILE):
        return
    try:
        with open(COOKIE_FILE) as f:
            raw = json.load(f)
        if raw and _browser:
            cookies = _convert_cookies(raw)
            await _browser.add_cookies(cookies)
    except Exception:
        pass


async def inject_cookies(cookies: list[dict]):
    """Inject cookies into the running browser context."""
    global _browser
    if _browser_alive():
        converted = _convert_cookies(cookies)
        await _browser.add_cookies(converted)
        logger.info(f"Injected {len(converted)} cookies into browser context")
    else:
        logger.warning("inject_cookies called but browser not alive — cookies saved to file only")


async def scrape_lot(url: str) -> LotDetails:
    """Scrape a HiBid lot page for auction details using real selectors."""
    browser = await get_browser()
    page = browser.pages[0] if browser.pages else await browser.new_page()

    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    lot_id = parse_lot_id_from_url(url)

    # Real HiBid selectors discovered from live pages
    title_text = await _safe_text(page, "h1") or "Unknown"
    # Title format: "Lot # : 1 - OB-Children's magnetic fortress HL83306"
    # Strip the "Lot # : N - " prefix if present
    title = re.sub(r"^Lot\s*#\s*:\s*\d+\s*-\s*", "", title_text).strip()

    # Current bid: ".lot-high-bid" -> "High Bid: 16.00 CAD"
    bid_text = await _safe_text(page, ".lot-high-bid") or "$0"
    current_bid = parse_price_from_text(bid_text)

    # Bid button contains next bid amount: ".lot-bid-button" -> "Bid 18.00 CAD"
    bid_btn_text = await _safe_text(page, ".lot-bid-button") or ""
    next_bid = parse_price_from_text(bid_btn_text) if bid_btn_text else 0
    increment = (next_bid - current_bid) if next_bid > current_bid else 5.0

    # Time: prefer exact close time from Apollo SSR state, fall back to DOM text
    time_text = await page.evaluate("""() => {
        // Extract exact close time from HiBid's embedded Apollo state
        const scripts = document.querySelectorAll('script');
        for (const s of scripts) {
            const text = s.textContent;
            if (!text.includes('timeLeftTitle')) continue;
            // "Internet Bidding closes at: 3/17/2026 8:56:30 PM EST"
            const m = text.match(/"timeLeftTitle"\\s*:\\s*"Internet Bidding closes at:\\s*([^"]+)"/);
            if (m) return 'CLOSES_AT:' + m[1].trim();
            // fallback: timeLeftSeconds
            const s2 = text.match(/"timeLeftSeconds"\\s*:\\s*([\\d.]+)/);
            if (s2) return 'SECS:' + s2[1];
            break;
        }
        // DOM fallback
        const el = document.querySelector('.lot-time-left');
        return el ? el.innerText.trim() : '';
    }""") or ""

    # Thumbnail — HiBid uses background-image on divs, not <img> tags
    thumbnail_url = await page.evaluate("""() => {
        const el = document.querySelector("[style*='background-image'][style*='cdn.hibid.com']");
        if (!el) return null;
        const m = el.style.backgroundImage.match(/url\\("?([^"\\)]+)"?\\)/);
        return m ? m[1] : null;
    }""") or None

    # Auction house name (from page header/branding)
    house_text = await _safe_text(page, "[class*='auctioneer'], [class*='auction-house'], .company-name") or ""

    return LotDetails(
        lot_id=lot_id,
        title=title,
        current_bid=current_bid,
        increment=increment,
        premium_pct=None,
        end_time=time_text.strip() if time_text else None,
        thumbnail_url=thumbnail_url,
        auction_house=house_text.strip() if house_text else None,
        lot_url=url,
    )


async def _safe_text(page: Page, selector: str) -> str | None:
    """Safely extract inner text from the first matching element."""
    try:
        el = await page.query_selector(selector)
        if el:
            return await el.inner_text()
    except Exception:
        pass
    return None
