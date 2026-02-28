# HiBid Deal Analyzer & Snipe Bot - Implementation Plan

**Goal:** Build a local web app that calculates true auction costs, compares prices against eBay, and auto-snipes HiBid auctions at minimum increment through soft close.

**Architecture:** FastAPI backend with SQLite, vanilla JS frontend, Playwright for HiBid browser automation. Runs in Docker on 192.168.1.121. Discord webhooks for notifications.

**Tech Stack:** Python 3.12, FastAPI, Playwright, SQLite, eBay Browse API, Discord webhooks, Docker

---

### Task 1: Project Scaffolding & Database

**Files:**
- Create: `backend/__init__.py`
- Create: `backend/db.py`
- Create: `backend/models.py`
- Create: `tests/__init__.py`
- Create: `tests/test_db.py`
- Create: `requirements.txt`

**Step 1: Create requirements.txt**

```
fastapi==0.115.0
uvicorn[standard]==0.30.0
sqlalchemy==2.0.35
aiosqlite==0.20.0
httpx==0.27.0
playwright==1.48.0
python-dotenv==1.0.1
pydantic==2.9.0
pytest==8.3.0
pytest-asyncio==0.24.0
```

**Step 2: Write the failing test for database models**

```python
# tests/test_db.py
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.db import Base, get_engine
from backend.models import AuctionHouse, Snipe, DealCheck

def test_create_auction_house():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        house = AuctionHouse(name="Burlington Auction Centre", premium_pct=15.0)
        session.add(house)
        session.commit()
        session.refresh(house)
        assert house.id == 1
        assert house.name == "Burlington Auction Centre"
        assert house.premium_pct == 15.0

def test_create_snipe():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        house = AuctionHouse(name="Test House", premium_pct=14.0)
        session.add(house)
        session.commit()
        snipe = Snipe(
            lot_url="https://hibid.com/lot/123/test-item",
            lot_title="Test Item",
            lot_id="123",
            max_cap=100.0,
            current_bid=50.0,
            increment=5.0,
            status="watching",
            auction_house_id=house.id,
        )
        session.add(snipe)
        session.commit()
        session.refresh(snipe)
        assert snipe.id == 1
        assert snipe.status == "watching"
        assert snipe.max_cap == 100.0

def test_create_deal_check():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        house = AuctionHouse(name="Test House", premium_pct=16.0)
        session.add(house)
        session.commit()
        deal = DealCheck(
            item_name="Milwaukee M18 Drill",
            bid_price=80.0,
            true_cost=104.52,
            ebay_avg_sold=120.0,
            ebay_low=90.0,
            ebay_high=150.0,
            verdict="good_deal",
            auction_house_id=house.id,
        )
        session.add(deal)
        session.commit()
        assert deal.id == 1
        assert deal.verdict == "good_deal"
```

**Step 3: Run test to verify it fails**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_db.py -v`
Expected: FAIL (modules don't exist yet)

**Step 4: Implement database models**

```python
# backend/__init__.py
```

```python
# backend/db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session
import os

class Base(DeclarativeBase):
    pass

DB_PATH = os.environ.get("HIBID_DB_PATH", "hibid_sniper.db")

def get_engine(db_path: str = None):
    path = db_path or DB_PATH
    return create_engine(f"sqlite:///{path}")

def get_session(engine=None) -> Session:
    if engine is None:
        engine = get_engine()
    return Session(engine)

def init_db(engine=None):
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
```

```python
# backend/models.py
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.sql import func
from backend.db import Base

class AuctionHouse(Base):
    __tablename__ = "auction_houses"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    premium_pct = Column(Float, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

class Snipe(Base):
    __tablename__ = "snipes"
    id = Column(Integer, primary_key=True)
    lot_url = Column(String, nullable=False)
    lot_title = Column(String)
    lot_id = Column(String)
    thumbnail_url = Column(String)
    max_cap = Column(Float, nullable=False)
    current_bid = Column(Float)
    increment = Column(Float)
    our_last_bid = Column(Float)
    status = Column(String, default="watching")  # watching, bidding, won, lost, capped_out, cancelled
    end_time = Column(DateTime)
    auction_house_id = Column(Integer, ForeignKey("auction_houses.id"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class DealCheck(Base):
    __tablename__ = "deal_checks"
    id = Column(Integer, primary_key=True)
    item_name = Column(String, nullable=False)
    bid_price = Column(Float, nullable=False)
    true_cost = Column(Float)
    ebay_avg_sold = Column(Float)
    ebay_low = Column(Float)
    ebay_high = Column(Float)
    ebay_results = Column(Text)  # JSON string of listings
    amazon_search_url = Column(String)
    verdict = Column(String)  # good_deal, fair, overpriced
    auction_house_id = Column(Integer, ForeignKey("auction_houses.id"))
    created_at = Column(DateTime, server_default=func.now())
```

```python
# tests/__init__.py
```

**Step 5: Run test to verify it passes**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_db.py -v`
Expected: All 3 tests PASS

**Step 6: Commit**

```bash
cd /home/htpc/hibid-sniper
git init
git add requirements.txt backend/ tests/
git commit -m "feat: project scaffolding with SQLite models for auction houses, snipes, and deal checks"
```

---

### Task 2: True Cost Calculator

**Files:**
- Create: `backend/calculator.py`
- Create: `tests/test_calculator.py`

**Step 1: Write the failing tests**

```python
# tests/test_calculator.py
from backend.calculator import calculate_true_cost, get_verdict

HST_RATE = 0.13

def test_basic_true_cost():
    result = calculate_true_cost(bid_price=100.0, premium_pct=15.0)
    # 100 + 15% = 115, then 115 * 1.13 = 129.95
    assert result["bid_price"] == 100.0
    assert result["premium_amount"] == 15.0
    assert result["subtotal"] == 115.0
    assert result["tax_amount"] == 14.95
    assert result["total"] == 129.95

def test_true_cost_different_premium():
    result = calculate_true_cost(bid_price=200.0, premium_pct=16.0)
    # 200 + 32 = 232, then 232 * 1.13 = 262.16
    assert result["premium_amount"] == 32.0
    assert result["subtotal"] == 232.0
    assert result["total"] == 262.16

def test_true_cost_zero_bid():
    result = calculate_true_cost(bid_price=0.0, premium_pct=15.0)
    assert result["total"] == 0.0

def test_verdict_good_deal():
    # True cost well below eBay average
    verdict = get_verdict(true_cost=80.0, ebay_avg_sold=120.0)
    assert verdict == "good_deal"

def test_verdict_fair():
    # True cost close to eBay average (within 15%)
    verdict = get_verdict(true_cost=110.0, ebay_avg_sold=120.0)
    assert verdict == "fair"

def test_verdict_overpriced():
    # True cost above eBay average
    verdict = get_verdict(true_cost=140.0, ebay_avg_sold=120.0)
    assert verdict == "overpriced"

def test_verdict_no_ebay_data():
    verdict = get_verdict(true_cost=100.0, ebay_avg_sold=None)
    assert verdict == "unknown"
```

**Step 2: Run test to verify it fails**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_calculator.py -v`
Expected: FAIL

**Step 3: Implement calculator**

```python
# backend/calculator.py

HST_RATE = 0.13

def calculate_true_cost(bid_price: float, premium_pct: float) -> dict:
    premium_amount = round(bid_price * (premium_pct / 100), 2)
    subtotal = round(bid_price + premium_amount, 2)
    tax_amount = round(subtotal * HST_RATE, 2)
    total = round(subtotal + tax_amount, 2)
    return {
        "bid_price": bid_price,
        "premium_pct": premium_pct,
        "premium_amount": premium_amount,
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
```

**Step 4: Run test to verify it passes**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_calculator.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add backend/calculator.py tests/test_calculator.py
git commit -m "feat: true cost calculator with HST and deal verdict logic"
```

---

### Task 3: eBay Price Lookup

**Files:**
- Create: `backend/ebay.py`
- Create: `tests/test_ebay.py`

**Note:** eBay Browse API requires OAuth. For initial development we'll use eBay's public search and scrape completed/sold listings. This avoids the API approval wait and works immediately. Can upgrade to official API later.

**Step 1: Write the failing tests**

```python
# tests/test_ebay.py
import pytest
from backend.ebay import build_ebay_search_url, build_ebay_sold_url, parse_price, build_amazon_search_url

def test_build_ebay_search_url():
    url = build_ebay_search_url("Milwaukee M18 Drill")
    assert "ebay.ca" in url or "ebay.com" in url
    assert "Milwaukee" in url
    assert "M18" in url
    assert "Drill" in url

def test_build_ebay_sold_url():
    url = build_ebay_sold_url("Milwaukee M18 Drill")
    assert "LH_Complete=1" in url
    assert "LH_Sold=1" in url

def test_build_amazon_search_url():
    url = build_amazon_search_url("Milwaukee M18 Drill")
    assert "amazon.ca" in url
    assert "Milwaukee" in url

def test_parse_price_simple():
    assert parse_price("$129.99") == 129.99

def test_parse_price_with_cad():
    assert parse_price("C $129.99") == 129.99

def test_parse_price_with_commas():
    assert parse_price("$1,299.99") == 1299.99

def test_parse_price_none():
    assert parse_price("") is None
    assert parse_price(None) is None
```

**Step 2: Run test to verify it fails**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_ebay.py -v`
Expected: FAIL

**Step 3: Implement eBay module**

```python
# backend/ebay.py
import re
import urllib.parse
import httpx
from dataclasses import dataclass

@dataclass
class EbayListing:
    title: str
    price: float
    url: str
    sold: bool = False

def build_ebay_search_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.ebay.ca/sch/i.html?_nkw={encoded}&_sop=15"

def build_ebay_sold_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.ebay.ca/sch/i.html?_nkw={encoded}&LH_Complete=1&LH_Sold=1&_sop=15"

def build_amazon_search_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.amazon.ca/s?k={encoded}"

def parse_price(price_str: str | None) -> float | None:
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", price_str.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None

async def search_ebay(query: str) -> dict:
    """Scrape eBay search results for active and sold listings."""
    active_url = build_ebay_search_url(query)
    sold_url = build_ebay_sold_url(query)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        active_resp = await client.get(active_url)
        sold_resp = await client.get(sold_url)

    active_listings = _parse_ebay_results(active_resp.text, sold=False)
    sold_listings = _parse_ebay_results(sold_resp.text, sold=True)

    active_prices = [l.price for l in active_listings if l.price]
    sold_prices = [l.price for l in sold_listings if l.price]

    return {
        "active": {
            "count": len(active_prices),
            "low": min(active_prices) if active_prices else None,
            "high": max(active_prices) if active_prices else None,
            "avg": round(sum(active_prices) / len(active_prices), 2) if active_prices else None,
            "listings": [{"title": l.title, "price": l.price, "url": l.url} for l in active_listings[:10]],
            "search_url": active_url,
        },
        "sold": {
            "count": len(sold_prices),
            "low": min(sold_prices) if sold_prices else None,
            "high": max(sold_prices) if sold_prices else None,
            "avg": round(sum(sold_prices) / len(sold_prices), 2) if sold_prices else None,
            "listings": [{"title": l.title, "price": l.price, "url": l.url} for l in sold_listings[:10]],
            "search_url": sold_url,
        },
        "amazon_url": build_amazon_search_url(query),
    }

def _parse_ebay_results(html: str, sold: bool = False) -> list[EbayListing]:
    """Parse eBay search results HTML for listings."""
    listings = []
    # Pattern to find listing items - eBay uses s-item class
    item_pattern = re.compile(
        r'<div class="s-item__wrapper.*?</div>\s*</div>\s*</div>',
        re.DOTALL,
    )
    title_pattern = re.compile(r'<div class="s-item__title"><span[^>]*>(.*?)</span>')
    price_pattern = re.compile(r'<span class="s-item__price">(.*?)</span>')
    link_pattern = re.compile(r'<a[^>]*class="s-item__link"[^>]*href="([^"]*)"')

    for match in item_pattern.finditer(html):
        block = match.group(0)
        title_match = title_pattern.search(block)
        price_match = price_pattern.search(block)
        link_match = link_pattern.search(block)

        if title_match and price_match and link_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
            if title.lower() == "shop on ebay":
                continue
            price = parse_price(price_match.group(1))
            url = link_match.group(1).split("?")[0]
            if price:
                listings.append(EbayListing(title=title, price=price, url=url, sold=sold))

    return listings
```

**Step 4: Run test to verify it passes**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_ebay.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add backend/ebay.py tests/test_ebay.py
git commit -m "feat: eBay price lookup with search URL builders and HTML scraping"
```

---

### Task 4: Discord Notifications

**Files:**
- Create: `backend/discord_notify.py`
- Create: `tests/test_discord.py`

**Step 1: Write the failing tests**

```python
# tests/test_discord.py
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
    assert msg["embeds"][0]["color"] == 0x00FF00  # green

def test_format_snipe_lost():
    msg = format_snipe_lost(
        lot_title="Some Item",
        lot_url="https://hibid.com/lot/456/test",
        final_price=120.0,
        your_cap=100.0,
    )
    assert msg["embeds"][0]["color"] == 0xFF0000  # red

def test_format_snipe_capped():
    msg = format_snipe_capped(
        lot_title="Expensive Thing",
        lot_url="https://hibid.com/lot/789/test",
        current_price=105.0,
        your_cap=100.0,
    )
    assert "Capped Out" in msg["embeds"][0]["title"]
```

**Step 2: Run test to verify it fails**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_discord.py -v`
Expected: FAIL

**Step 3: Implement Discord notifications**

```python
# backend/discord_notify.py
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
```

**Step 4: Run test to verify it passes**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_discord.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add backend/discord_notify.py tests/test_discord.py
git commit -m "feat: Discord webhook notifications for snipe outcomes"
```

---

### Task 5: HiBid Scraper (Lot Details)

**Files:**
- Create: `backend/hibid_scraper.py`
- Create: `tests/test_hibid_scraper.py`

**Step 1: Write the failing tests**

```python
# tests/test_hibid_scraper.py
from backend.hibid_scraper import parse_lot_id_from_url, parse_increment, parse_premium_from_text

def test_parse_lot_id():
    url = "https://www.hibid.com/lot/284765708/john-deere-lp-72-land-plane-attachment"
    assert parse_lot_id_from_url(url) == "284765708"

def test_parse_lot_id_trailing_slash():
    url = "https://www.hibid.com/lot/123456/some-item/"
    assert parse_lot_id_from_url(url) == "123456"

def test_parse_lot_id_with_params():
    url = "https://www.hibid.com/lot/123456/some-item?ref=search"
    assert parse_lot_id_from_url(url) == "123456"

def test_parse_increment():
    assert parse_increment("$2.00") == 2.0
    assert parse_increment("$5.00") == 5.0
    assert parse_increment("$10.00") == 10.0

def test_parse_premium_from_text():
    text = "Buyer's Premium: 15%"
    assert parse_premium_from_text(text) == 15.0

def test_parse_premium_from_text_variant():
    text = "A 16% buyer's premium applies"
    assert parse_premium_from_text(text) == 16.0
```

**Step 2: Run test to verify it fails**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_hibid_scraper.py -v`
Expected: FAIL

**Step 3: Implement HiBid scraper utilities**

```python
# backend/hibid_scraper.py
import re
from urllib.parse import urlparse
from dataclasses import dataclass
from playwright.async_api import async_playwright, Page, Browser

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
    path = urlparse(url).path.rstrip("/")
    parts = path.split("/")
    # /lot/{id}/{slug}
    lot_idx = parts.index("lot")
    return parts[lot_idx + 1]

def parse_increment(text: str) -> float:
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    return float(cleaned)

def parse_premium_from_text(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if match:
        return float(match.group(1))
    return None

# Browser instance management
_browser: Browser | None = None
PROFILE_DIR = "/home/htpc/hibid-sniper/browser_profile"

async def get_browser() -> Browser:
    global _browser
    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
    return _browser

async def scrape_lot(url: str) -> LotDetails:
    """Scrape a HiBid lot page for auction details."""
    browser = await get_browser()
    page = browser.pages[0] if browser.pages else await browser.new_page()

    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)  # Let Angular render

    lot_id = parse_lot_id_from_url(url)

    # Extract data from page
    title = await _safe_text(page, "h1, .lot-title, [class*='title']") or "Unknown"
    bid_text = await _safe_text(page, "[class*='current-bid'], [class*='high-bid'], .bid-amount") or "$0"
    increment_text = await _safe_text(page, "[class*='increment'], [class*='bid-increment']") or "$5"
    premium_text = await _safe_text(page, "[class*='premium'], [class*='buyers-premium']") or ""
    time_text = await _safe_text(page, "[class*='countdown'], [class*='time-left'], [class*='end-time']") or ""
    house_text = await _safe_text(page, "[class*='auctioneer'], [class*='auction-house']") or ""

    # Try to get thumbnail
    thumb = await page.query_selector("img[class*='lot'], img[class*='gallery'], .lot-image img")
    thumbnail_url = await thumb.get_attribute("src") if thumb else None

    current_bid = float(re.sub(r"[^\d.]", "", bid_text.replace(",", "")) or "0")
    increment = parse_increment(increment_text) if increment_text else 5.0
    premium_pct = parse_premium_from_text(premium_text)

    return LotDetails(
        lot_id=lot_id,
        title=title.strip(),
        current_bid=current_bid,
        increment=increment,
        premium_pct=premium_pct,
        end_time=time_text.strip() if time_text else None,
        thumbnail_url=thumbnail_url,
        auction_house=house_text.strip() if house_text else None,
        lot_url=url,
    )

async def _safe_text(page: Page, selector: str) -> str | None:
    try:
        el = await page.query_selector(selector)
        if el:
            return await el.inner_text()
    except Exception:
        pass
    return None
```

**Step 4: Run test to verify it passes**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_hibid_scraper.py -v`
Expected: All 6 tests PASS (only testing utility functions, not browser scraping)

**Step 5: Commit**

```bash
git add backend/hibid_scraper.py tests/test_hibid_scraper.py
git commit -m "feat: HiBid lot scraper with URL parsing and Playwright browser automation"
```

---

### Task 6: Snipe Bot Engine

**Files:**
- Create: `backend/sniper.py`
- Create: `tests/test_sniper.py`

**Step 1: Write the failing tests**

```python
# tests/test_sniper.py
from backend.sniper import should_bid, next_bid_amount

def test_should_bid_under_cap():
    assert should_bid(current_price=50.0, max_cap=100.0, increment=5.0) is True

def test_should_not_bid_at_cap():
    assert should_bid(current_price=100.0, max_cap=100.0, increment=5.0) is False

def test_should_not_bid_over_cap():
    assert should_bid(current_price=95.0, max_cap=100.0, increment=10.0) is False

def test_should_bid_exact_cap():
    # current 95, increment 5 = next bid 100, which equals cap
    assert should_bid(current_price=95.0, max_cap=100.0, increment=5.0) is True

def test_next_bid_amount():
    assert next_bid_amount(current_price=50.0, increment=5.0) == 55.0

def test_next_bid_amount_large_increment():
    assert next_bid_amount(current_price=200.0, increment=10.0) == 210.0
```

**Step 2: Run test to verify it fails**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_sniper.py -v`
Expected: FAIL

**Step 3: Implement sniper logic**

```python
# backend/sniper.py
import asyncio
import logging
from datetime import datetime, timezone
from backend.hibid_scraper import get_browser, parse_lot_id_from_url, LotDetails
from backend.calculator import calculate_true_cost
from backend.discord_notify import (
    send_notification, format_snipe_won, format_snipe_lost, format_snipe_capped,
)

logger = logging.getLogger(__name__)

# How many seconds before close to place bid
SNIPE_WINDOW_SECONDS = 3
# How often to poll the auction page (seconds)
POLL_INTERVAL = 5
# After soft close extension, wait this long before re-checking
SOFT_CLOSE_RECHECK = 2

def should_bid(current_price: float, max_cap: float, increment: float) -> bool:
    next_price = current_price + increment
    return next_price <= max_cap

def next_bid_amount(current_price: float, increment: float) -> float:
    return current_price + increment

class SnipeJob:
    def __init__(self, lot_url: str, max_cap: float, premium_pct: float, snipe_id: int | None = None):
        self.lot_url = lot_url
        self.max_cap = max_cap
        self.premium_pct = premium_pct
        self.snipe_id = snipe_id
        self.status = "watching"
        self.cancelled = False

    async def run(self, on_status_change=None):
        """Main snipe loop. Watches auction and bids at the last moment."""
        browser = await get_browser()
        page = await browser.new_page()

        try:
            await page.goto(self.lot_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            while not self.cancelled:
                # Get current auction state
                state = await self._get_auction_state(page)
                if state is None:
                    logger.error("Could not read auction state")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                current_price = state["current_price"]
                increment = state["increment"]
                seconds_left = state["seconds_left"]
                is_ended = state["is_ended"]

                if is_ended:
                    # Check if we won
                    if state.get("we_are_winning"):
                        self.status = "won"
                        cost = calculate_true_cost(current_price, self.premium_pct)
                        await send_notification(format_snipe_won(
                            lot_title=state.get("title", "Unknown"),
                            lot_url=self.lot_url,
                            winning_bid=current_price,
                            true_cost=cost["total"],
                        ))
                    else:
                        self.status = "lost"
                        await send_notification(format_snipe_lost(
                            lot_title=state.get("title", "Unknown"),
                            lot_url=self.lot_url,
                            final_price=current_price,
                            your_cap=self.max_cap,
                        ))
                    break

                if not should_bid(current_price, self.max_cap, increment):
                    self.status = "capped_out"
                    await send_notification(format_snipe_capped(
                        lot_title=state.get("title", "Unknown"),
                        lot_url=self.lot_url,
                        current_price=current_price,
                        your_cap=self.max_cap,
                    ))
                    break

                # Wait until snipe window
                if seconds_left > SNIPE_WINDOW_SECONDS:
                    wait_time = min(seconds_left - SNIPE_WINDOW_SECONDS, POLL_INTERVAL)
                    await asyncio.sleep(wait_time)
                    continue

                # Time to bid!
                self.status = "bidding"
                if on_status_change:
                    await on_status_change(self)

                bid_amount = next_bid_amount(current_price, increment)
                logger.info(f"Sniping {self.lot_url} at ${bid_amount}")
                success = await self._place_bid(page, bid_amount)

                if success:
                    # Wait for soft close to settle, then recheck
                    await asyncio.sleep(SOFT_CLOSE_RECHECK)
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
                else:
                    logger.warning("Bid placement may have failed, rechecking...")
                    await asyncio.sleep(SOFT_CLOSE_RECHECK)

        finally:
            await page.close()

        if on_status_change:
            await on_status_change(self)
        return self.status

    async def _get_auction_state(self, page) -> dict | None:
        """Read current price, increment, and time remaining from the page."""
        try:
            # This will need tuning to HiBid's actual DOM structure
            state = await page.evaluate("""() => {
                const getText = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? el.innerText.trim() : null;
                };
                return {
                    current_price: getText('[class*="current-bid"], [class*="high-bid"]'),
                    increment: getText('[class*="increment"]'),
                    time_left: getText('[class*="countdown"], [class*="time-left"]'),
                    title: getText('h1, [class*="lot-title"]'),
                    bid_button: getText('[class*="bid-button"], button[type="submit"]'),
                };
            }""")

            if not state or not state.get("current_price"):
                return None

            import re
            price = float(re.sub(r"[^\d.]", "", state["current_price"].replace(",", "")) or "0")
            inc_text = state.get("increment") or "$5"
            increment = float(re.sub(r"[^\d.]", "", inc_text.replace(",", "")) or "5")

            # Parse time remaining (rough: look for seconds)
            time_text = state.get("time_left") or ""
            seconds_left = self._parse_time_remaining(time_text)

            return {
                "current_price": price,
                "increment": increment,
                "seconds_left": seconds_left,
                "is_ended": seconds_left <= 0 and "ended" in time_text.lower(),
                "title": state.get("title", "Unknown"),
                "we_are_winning": False,  # TODO: detect from page
            }
        except Exception as e:
            logger.error(f"Error reading auction state: {e}")
            return None

    def _parse_time_remaining(self, text: str) -> float:
        """Parse countdown text to seconds remaining."""
        import re
        total = 0
        days = re.search(r"(\d+)\s*d", text)
        hours = re.search(r"(\d+)\s*h", text)
        minutes = re.search(r"(\d+)\s*m", text)
        seconds = re.search(r"(\d+)\s*s", text)
        if days:
            total += int(days.group(1)) * 86400
        if hours:
            total += int(hours.group(1)) * 3600
        if minutes:
            total += int(minutes.group(1)) * 60
        if seconds:
            total += int(seconds.group(1))
        return total

    async def _place_bid(self, page, amount: float) -> bool:
        """Click the bid button on HiBid."""
        try:
            # Look for bid input and button - will need tuning to actual HiBid DOM
            bid_button = await page.query_selector(
                'button[class*="bid"], button:has-text("Bid"), input[type="submit"][value*="Bid"]'
            )
            if bid_button:
                await bid_button.click()
                await page.wait_for_timeout(1000)
                # Confirm if there's a confirmation dialog
                confirm = await page.query_selector(
                    'button:has-text("Confirm"), button:has-text("Yes"), button:has-text("OK")'
                )
                if confirm:
                    await confirm.click()
                    await page.wait_for_timeout(1000)
                return True
            logger.warning("Could not find bid button")
            return False
        except Exception as e:
            logger.error(f"Error placing bid: {e}")
            return False

    def cancel(self):
        self.cancelled = True
        self.status = "cancelled"
```

**Step 4: Run test to verify it passes**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_sniper.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add backend/sniper.py tests/test_sniper.py
git commit -m "feat: snipe bot engine with soft close handling and minimum increment bidding"
```

---

### Task 7: FastAPI Server

**Files:**
- Create: `backend/main.py`
- Create: `tests/test_api.py`
- Create: `.env.example`

**Step 1: Write the failing tests**

```python
# tests/test_api.py
import pytest
from fastapi.testclient import TestClient
from backend.main import app, init_app_db

@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    import os
    os.environ["HIBID_DB_PATH"] = str(tmp_path / "test.db")
    init_app_db()

def test_health():
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

def test_calculate_cost():
    client = TestClient(app)
    resp = client.post("/api/calculate", json={"bid_price": 100, "premium_pct": 15})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 129.95

def test_crud_auction_house():
    client = TestClient(app)
    # Create
    resp = client.post("/api/auction-houses", json={"name": "Test House", "premium_pct": 15.0})
    assert resp.status_code == 200
    house_id = resp.json()["id"]

    # List
    resp = client.get("/api/auction-houses")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # Delete
    resp = client.delete(f"/api/auction-houses/{house_id}")
    assert resp.status_code == 200

def test_ebay_search():
    client = TestClient(app)
    resp = client.get("/api/search-ebay?query=Milwaukee+M18+Drill")
    assert resp.status_code == 200
    data = resp.json()
    assert "active" in data
    assert "sold" in data
    assert "amazon_url" in data
```

**Step 2: Run test to verify it fails**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_api.py -v`
Expected: FAIL

**Step 3: Implement FastAPI server**

```python
# backend/main.py
import asyncio
import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()

from backend.db import Base, get_engine, init_db
from backend.models import AuctionHouse, Snipe, DealCheck
from backend.calculator import calculate_true_cost, get_verdict
from backend.ebay import search_ebay, build_amazon_search_url
from backend.sniper import SnipeJob

# Active snipe jobs
active_snipes: dict[int, SnipeJob] = {}

def init_app_db():
    engine = get_engine()
    init_db(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_app_db()
    yield
    # Cancel active snipes on shutdown
    for job in active_snipes.values():
        job.cancel()

app = FastAPI(title="HiBid Sniper", lifespan=lifespan)

# --- Pydantic models ---

class CalcRequest(BaseModel):
    bid_price: float
    premium_pct: float

class AuctionHouseCreate(BaseModel):
    name: str
    premium_pct: float

class SnipeCreate(BaseModel):
    lot_url: str
    max_cap: float
    auction_house_id: int

# --- API Routes ---

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.post("/api/calculate")
def calculate(req: CalcRequest):
    return calculate_true_cost(req.bid_price, req.premium_pct)

@app.get("/api/search-ebay")
async def ebay_search(query: str):
    return await search_ebay(query)

@app.post("/api/deal-check")
async def deal_check(item_name: str, bid_price: float, auction_house_id: int):
    engine = get_engine()
    with Session(engine) as session:
        house = session.get(AuctionHouse, auction_house_id)
        if not house:
            raise HTTPException(404, "Auction house not found")

        cost = calculate_true_cost(bid_price, house.premium_pct)
        ebay = await search_ebay(item_name)
        ebay_avg = ebay["sold"]["avg"]
        verdict = get_verdict(cost["total"], ebay_avg)

        deal = DealCheck(
            item_name=item_name,
            bid_price=bid_price,
            true_cost=cost["total"],
            ebay_avg_sold=ebay_avg,
            ebay_low=ebay["sold"]["low"],
            ebay_high=ebay["sold"]["high"],
            ebay_results=json.dumps(ebay),
            amazon_search_url=ebay["amazon_url"],
            verdict=verdict,
            auction_house_id=auction_house_id,
        )
        session.add(deal)
        session.commit()
        session.refresh(deal)

        return {
            "deal_id": deal.id,
            "cost": cost,
            "ebay": ebay,
            "verdict": verdict,
        }

# --- Auction Houses CRUD ---

@app.get("/api/auction-houses")
def list_houses():
    engine = get_engine()
    with Session(engine) as session:
        houses = session.query(AuctionHouse).all()
        return [{"id": h.id, "name": h.name, "premium_pct": h.premium_pct} for h in houses]

@app.post("/api/auction-houses")
def create_house(req: AuctionHouseCreate):
    engine = get_engine()
    with Session(engine) as session:
        house = AuctionHouse(name=req.name, premium_pct=req.premium_pct)
        session.add(house)
        session.commit()
        session.refresh(house)
        return {"id": house.id, "name": house.name, "premium_pct": house.premium_pct}

@app.delete("/api/auction-houses/{house_id}")
def delete_house(house_id: int):
    engine = get_engine()
    with Session(engine) as session:
        house = session.get(AuctionHouse, house_id)
        if not house:
            raise HTTPException(404, "Not found")
        session.delete(house)
        session.commit()
        return {"ok": True}

# --- Snipe Routes ---

@app.get("/api/snipes")
def list_snipes():
    engine = get_engine()
    with Session(engine) as session:
        snipes = session.query(Snipe).filter(Snipe.status != "cancelled").all()
        return [{
            "id": s.id,
            "lot_url": s.lot_url,
            "lot_title": s.lot_title,
            "max_cap": s.max_cap,
            "current_bid": s.current_bid,
            "status": s.status,
            "thumbnail_url": s.thumbnail_url,
        } for s in snipes]

@app.post("/api/snipes")
async def create_snipe(req: SnipeCreate):
    engine = get_engine()
    with Session(engine) as session:
        house = session.get(AuctionHouse, req.auction_house_id)
        if not house:
            raise HTTPException(404, "Auction house not found")

        # Scrape lot details
        from backend.hibid_scraper import scrape_lot
        lot = await scrape_lot(req.lot_url)

        snipe = Snipe(
            lot_url=req.lot_url,
            lot_title=lot.title,
            lot_id=lot.lot_id,
            thumbnail_url=lot.thumbnail_url,
            max_cap=req.max_cap,
            current_bid=lot.current_bid,
            increment=lot.increment,
            status="watching",
            auction_house_id=req.auction_house_id,
        )
        session.add(snipe)
        session.commit()
        session.refresh(snipe)

        # Start snipe job in background
        job = SnipeJob(
            lot_url=req.lot_url,
            max_cap=req.max_cap,
            premium_pct=house.premium_pct,
            snipe_id=snipe.id,
        )
        active_snipes[snipe.id] = job

        async def update_status(j: SnipeJob):
            with Session(engine) as s:
                db_snipe = s.get(Snipe, snipe.id)
                if db_snipe:
                    db_snipe.status = j.status
                    s.commit()

        asyncio.create_task(job.run(on_status_change=update_status))

        return {
            "id": snipe.id,
            "lot_title": lot.title,
            "current_bid": lot.current_bid,
            "increment": lot.increment,
            "status": "watching",
        }

@app.post("/api/snipes/{snipe_id}/cancel")
def cancel_snipe(snipe_id: int):
    if snipe_id in active_snipes:
        active_snipes[snipe_id].cancel()
        del active_snipes[snipe_id]
    engine = get_engine()
    with Session(engine) as session:
        snipe = session.get(Snipe, snipe_id)
        if snipe:
            snipe.status = "cancelled"
            session.commit()
    return {"ok": True}

# --- History ---

@app.get("/api/history")
def get_history():
    engine = get_engine()
    with Session(engine) as session:
        deals = session.query(DealCheck).order_by(DealCheck.created_at.desc()).limit(50).all()
        snipes = session.query(Snipe).filter(
            Snipe.status.in_(["won", "lost", "capped_out"])
        ).order_by(Snipe.updated_at.desc()).limit(50).all()
        return {
            "deals": [{
                "id": d.id, "item_name": d.item_name, "bid_price": d.bid_price,
                "true_cost": d.true_cost, "ebay_avg_sold": d.ebay_avg_sold, "verdict": d.verdict,
                "created_at": str(d.created_at),
            } for d in deals],
            "snipes": [{
                "id": s.id, "lot_title": s.lot_title, "lot_url": s.lot_url,
                "max_cap": s.max_cap, "current_bid": s.current_bid, "status": s.status,
                "created_at": str(s.created_at),
            } for s in snipes],
        }

# Serve frontend
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
```

```
# .env.example
HIBID_EMAIL=your_email@example.com
HIBID_PASSWORD=your_password
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK
HIBID_DB_PATH=hibid_sniper.db
```

**Step 4: Run test to verify it passes**

Run: `cd /home/htpc/hibid-sniper && python -m pytest tests/test_api.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add backend/main.py .env.example tests/test_api.py
git commit -m "feat: FastAPI server with deal check, snipe management, and auction house CRUD"
```

---

### Task 8: Frontend Dashboard

**Files:**
- Create: `frontend/index.html`

This is a single HTML file with embedded CSS and JS. Four tabs: Deal Analyzer, Snipe Queue, Auction Houses, History.

**Step 1: Create the frontend**

The full HTML file should include:
- Tab navigation (Deal Analyzer | Snipe Queue | Auction Houses | History)
- Deal Analyzer: form with item name, bid price, auction house dropdown, calculate button, results panel with cost breakdown + eBay data + verdict badge
- Snipe Queue: add snipe form (URL + max cap + house), active snipes table with status badges and cancel buttons, auto-refresh every 5s
- Auction Houses: add/delete houses with premium %
- History: tables of past deals and snipes
- Dark theme, clean modern UI
- All API calls via fetch() to /api/* endpoints

**Step 2: Test manually**

Run: `cd /home/htpc/hibid-sniper && uvicorn backend.main:app --host 0.0.0.0 --port 8199`
Open: http://192.168.1.121:8199
Verify: All 4 tabs load, forms submit, API calls work

**Step 3: Commit**

```bash
git add frontend/
git commit -m "feat: web dashboard with deal analyzer, snipe queue, auction houses, and history"
```

---

### Task 9: HiBid Login Flow

**Files:**
- Modify: `backend/hibid_scraper.py`
- Create: `backend/hibid_auth.py`

**Step 1: Implement HiBid authentication**

```python
# backend/hibid_auth.py
import os
import logging
from playwright.async_api import Page

logger = logging.getLogger(__name__)

HIBID_EMAIL = os.environ.get("HIBID_EMAIL", "")
HIBID_PASSWORD = os.environ.get("HIBID_PASSWORD", "")

async def login_if_needed(page: Page) -> bool:
    """Check if logged in, login if not. Returns True if logged in."""
    # Navigate to HiBid
    current_url = page.url
    if "hibid.com" not in current_url:
        await page.goto("https://www.hibid.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # Check if already logged in (look for account/profile element)
    logged_in = await page.query_selector('[class*="user-menu"], [class*="account"], [class*="logged-in"]')
    if logged_in:
        logger.info("Already logged in to HiBid")
        return True

    if not HIBID_EMAIL or not HIBID_PASSWORD:
        logger.error("HiBid credentials not configured")
        return False

    # Click login/sign-in button
    login_btn = await page.query_selector('a:has-text("Sign In"), a:has-text("Log In"), button:has-text("Sign In")')
    if login_btn:
        await login_btn.click()
        await page.wait_for_timeout(2000)

    # Fill credentials
    email_input = await page.query_selector('input[type="email"], input[name="email"], #email')
    password_input = await page.query_selector('input[type="password"], input[name="password"], #password')

    if email_input and password_input:
        await email_input.fill(HIBID_EMAIL)
        await page.wait_for_timeout(500)
        await password_input.fill(HIBID_PASSWORD)
        await page.wait_for_timeout(500)

        submit = await page.query_selector('button[type="submit"], button:has-text("Sign In"), button:has-text("Log In")')
        if submit:
            await submit.click()
            await page.wait_for_timeout(3000)

        # Verify login succeeded
        logged_in = await page.query_selector('[class*="user-menu"], [class*="account"], [class*="logged-in"]')
        if logged_in:
            logger.info("Successfully logged in to HiBid")
            return True

    logger.error("Failed to log in to HiBid")
    return False
```

**Step 2: Update sniper.py to call login before bidding**

Add `from backend.hibid_auth import login_if_needed` and call `await login_if_needed(page)` at the start of `SnipeJob.run()` before navigating to the lot.

**Step 3: Commit**

```bash
git add backend/hibid_auth.py backend/sniper.py
git commit -m "feat: HiBid login flow with persistent browser session"
```

---

### Task 10: Docker Deployment

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.env` (from .env.example, with real values)

**Step 1: Create Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY backend/ backend/
COPY frontend/ frontend/

EXPOSE 8199

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8199"]
```

**Step 2: Create docker-compose.yml**

```yaml
services:
  hibid-sniper:
    build: .
    container_name: hibid-sniper
    restart: unless-stopped
    ports:
      - "8199:8199"
    volumes:
      - ./data:/app/data
      - ./browser_profile:/app/browser_profile
    env_file:
      - .env
    environment:
      - HIBID_DB_PATH=/app/data/hibid_sniper.db
```

**Step 3: Build and test**

```bash
cd /home/htpc/hibid-sniper
mkdir -p data browser_profile
cp .env.example .env
# Edit .env with real credentials
docker compose build
docker compose up -d
# Test: curl http://localhost:8199/api/health
```

**Step 4: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example
git commit -m "feat: Docker deployment with persistent data and browser profile volumes"
```

---

### Task 11: Manual Testing & Tuning

This task is NOT TDD - it's manual integration testing against live HiBid.

**Step 1: Test Deal Analyzer end-to-end**
- Open http://192.168.1.121:8199
- Add an auction house (e.g. "Burlington Auction Centre", 15%)
- Search for an item you're interested in
- Verify cost calculation and eBay results look correct

**Step 2: Test HiBid scraping**
- Paste a real HiBid lot URL
- Verify it scrapes title, current bid, increment correctly
- If selectors are wrong, update `hibid_scraper.py` with correct CSS selectors from the actual page

**Step 3: Test login flow**
- Set your real HiBid credentials in .env
- Restart container
- Queue a snipe and verify it logs in successfully
- Watch the browser_profile directory for persistent cookies

**Step 4: Test Discord notifications**
- Set your Discord webhook URL in .env
- Manually trigger a test notification via the API or a test script

**Step 5: Tune snipe timing**
- Find an auction ending soon
- Queue a low-value snipe with a low max cap
- Watch the logs to verify timing is correct
- Adjust SNIPE_WINDOW_SECONDS if needed

**Step 6: Commit any fixes**

```bash
git add -A
git commit -m "fix: tuned HiBid selectors and snipe timing from live testing"
```
