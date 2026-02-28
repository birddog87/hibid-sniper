# HiBid Sniper - Code Guide

Detailed walkthrough of every module, function, and implementation detail.

---

## Directory Structure

```
hibid-sniper/
  backend/
    __init__.py              # Empty (package marker)
    main.py                  # FastAPI app, all REST endpoints, lifecycle, resume logic
    models.py                # SQLAlchemy ORM models (5 tables)
    db.py                    # Engine/session factory, table creation
    calculator.py            # True cost math + verdict logic
    ebay.py                  # Startpage proxy scraper + URL builders
    sniper.py                # SnipeJob class + two-phase scheduling + bid logic
    hibid_api.py             # Direct HiBid GraphQL API calls via httpx
    hibid_scraper.py         # Playwright lot page scraper
    hibid_auth.py            # HiBid login automation
    discord_notify.py        # Discord webhook message formatting
  frontend/
    index.html               # Entire SPA (HTML + CSS + JS in one file)
  tests/
    __init__.py              # Empty
    test_calculator.py       # 7 tests: cost calc + verdicts
    test_api.py              # 4 tests: health, calc, houses CRUD, eBay search
    test_ebay.py             # 7 tests: URL builders + price parsing
    test_sniper.py           # 6 tests: bid logic pure functions
    test_hibid_scraper.py    # Tests for scraper helpers
    test_db.py               # Database init tests
    test_discord.py          # Notification format tests
  docs/
    plans/                   # Design docs and implementation plans
    ARCHITECTURE.md
    API-REFERENCE.md
    SECURITY.md
    BUSINESS-LOGIC.md
    CODE-GUIDE.md            # This file
    DEPLOYMENT.md
  data/                      # Docker volume: SQLite database
  browser_profile/           # Docker volume: Playwright cookies
  Dockerfile
  docker-compose.yml
  .env                       # Credentials (gitignored)
  .gitignore
  requirements.txt
```

---

## Module-by-Module Breakdown

### `backend/db.py` - Database Layer

```python
# Declarative base for all models
class Base(DeclarativeBase):
    pass

# Engine factory - reads HIBID_DB_PATH env var each call
# Default: "hibid_sniper.db" in current directory
def get_engine(db_path=None) -> Engine:
    path = db_path or os.environ.get("HIBID_DB_PATH", "hibid_sniper.db")
    return create_engine(f"sqlite:///{path}")

# Session factory
def get_session(engine=None) -> Session:
    return Session(engine or get_engine())

# Idempotent table creation
def init_db(engine=None):
    Base.metadata.create_all(engine or get_engine())
```

**Design choice:** `get_engine()` reads env var on each call (not module-level) so test fixtures can override `HIBID_DB_PATH` to use temp databases.

---

### `backend/models.py` - Data Models

Five SQLAlchemy models mapping to SQLite tables:

**AuctionHouse** - Stores venue configurations
```python
class AuctionHouse(Base):
    __tablename__ = "auction_houses"
    id:          Mapped[int]      # Auto-increment PK
    name:        Mapped[str]      # "Burlington Auction Centre"
    premium_pct: Mapped[float]    # 15.0 (meaning 15%)
    created_at:  Mapped[datetime] # Server default: func.now()
```

**Snipe** - Tracks snipe jobs and their lifecycle
```python
class Snipe(Base):
    __tablename__ = "snipes"
    id:               Mapped[int]
    lot_url:          Mapped[str]       # Full HiBid URL
    lot_title:        Mapped[str|None]  # Scraped from page
    lot_id:           Mapped[str|None]  # Extracted from URL path
    thumbnail_url:    Mapped[str|None]  # Lot image
    max_cap:          Mapped[float]     # User's maximum bid
    current_bid:      Mapped[float|None]
    increment:        Mapped[float|None]
    our_last_bid:     Mapped[float|None]
    status:           Mapped[str]       # scheduled|watching|bidding|won|lost|capped_out|cancelled
    end_time:         Mapped[datetime|None]
    auction_house_id: Mapped[int]       # FK -> auction_houses.id
    created_at:       Mapped[datetime]
    updated_at:       Mapped[datetime]  # Auto-updates on change
```

**DealCheck** - Historical deal analyses
```python
class DealCheck(Base):
    __tablename__ = "deal_checks"
    id:               Mapped[int]
    item_name:        Mapped[str]
    bid_price:        Mapped[float]
    true_cost:        Mapped[float|None]
    ebay_avg_sold:    Mapped[float|None]
    ebay_low:         Mapped[float|None]
    ebay_high:        Mapped[float|None]
    ebay_results:     Mapped[str|None]   # JSON blob
    amazon_search_url: Mapped[str|None]
    verdict:          Mapped[str|None]
    auction_house_id: Mapped[int]        # FK -> auction_houses.id
    created_at:       Mapped[datetime]
```

**BidLog** - Records every bid placed (manual or automated)
```python
class BidLog(Base):
    __tablename__ = "bid_logs"
    id:          Mapped[int]
    snipe_id:    Mapped[int]       # FK -> snipes.id
    bid_amount:  Mapped[float]
    result:      Mapped[str|None]  # WINNING, OUTBID, ACCEPTED, NO_BID, error
    message:     Mapped[str|None]
    created_at:  Mapped[datetime]
```

**Settings** - App-wide configuration (single row)
```python
class Settings(Base):
    __tablename__ = "settings"
    id:              Mapped[int]
    global_spend_cap: Mapped[float]  # Default: 500.0
```

---

### `backend/calculator.py` - Cost Calculations

Two pure functions, no side effects, no I/O.

```python
HST_RATE = 0.13  # Ontario Harmonized Sales Tax

def calculate_true_cost(bid_price, premium_pct):
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

def get_verdict(true_cost, ebay_avg_sold):
    if ebay_avg_sold is None:
        return "unknown"
    ratio = true_cost / ebay_avg_sold
    if ratio <= 0.85:
        return "good_deal"
    elif ratio <= 1.10:
        return "fair"
    else:
        return "overpriced"
```

---

### `backend/ebay.py` - Price Discovery

**URL Builders** - Five functions that generate search URLs for different platforms. All use `urllib.parse.quote_plus()` for encoding.

**Startpage Proxy** - The main innovation. eBay blocks all server-side scraping. Startpage acts as a search proxy:

```python
async def search_ebay_via_startpage(query):
    async with httpx.AsyncClient() as client:
        # Search 1: Active eBay prices
        active_html = await _startpage_search(client, f"ebay.ca {query} price")
        active_listings = _parse_startpage_prices(active_html)

        # Search 2: Sold eBay prices
        sold_html = await _startpage_search(client, f"ebay.ca {query} sold completed price")
        sold_listings = _parse_startpage_prices(sold_html)

    # Aggregate into low/high/avg
    # Return with fallback search URLs for manual checking
```

**HTML Parser** - `_parse_startpage_prices()` is the most complex function:

1. Find all `<a>` tags with class containing `result-title` (Startpage result links)
2. Clean CSS artifacts from title text (Startpage embeds `<style>` and `@media` in results)
3. Find the next `<p>` with class containing `description` after each title
4. Regex extract CAD/USD prices from combined title+description text
5. Filter prices to $1-$50k range (removes shipping costs and junk)
6. Return first valid price per result (one price per listing)

**Key regex:** `(?:C\s*\$|CA\$|CAD\s*\$?|\$)\s*([\d,]+\.?\d*)`
Matches: `C $61.62`, `CA$115.56`, `$89.99`, `CAD $42.00`

---

### `backend/sniper.py` - Bid Automation (Two-Phase)

**Pure bid logic functions:**
```python
def should_bid(current_price, max_cap, increment):
    return current_price + increment <= max_cap

def next_bid_amount(current_price, increment):
    return current_price + increment
```

**Constants:**
```python
WAKE_BEFORE_SECONDS = 300   # Wake up 5 minutes before auction ends
SNIPE_WINDOW_SECONDS = 3    # Bid when <= 3 seconds remain
POLL_INTERVAL = 5           # Check auction state every 5 seconds
```

**SnipeJob class** - Manages the full lifecycle of a single snipe with two-phase execution.

**Constructor:** `SnipeJob(snipe_id, lot_url, max_cap, ..., end_time=None)`
- Accepts optional `end_time` (datetime) for scheduling

**`run()`** - Entry point, calls `_phase_sleep()` then `_phase_active()`:
1. **Phase 1 (`_phase_sleep`):** If `end_time` is set and > 5 minutes away, sleep in 60s chunks. Status = "scheduled". No browser opened. Checks cancellation flag each loop.
2. **Phase 2 (`_phase_active`):** Opens Playwright browser, monitors auction, places bids in final seconds. Status transitions: "watching" -> "bidding" -> "won"/"lost"/"capped_out".

Key implementation details:

1. **Browser reuse:** Gets shared Playwright browser via `get_browser()` singleton (Phase 2 only)
2. **Login on demand:** Calls `login_if_needed(page)` before starting monitor loop
3. **State reading:** Evaluates JavaScript on the HiBid page to extract auction state
4. **Time parsing:** Converts countdown strings like "2d 5h 30m 10s" to seconds
5. **Bid placement:** Clicks bid button, waits for confirmation dialog, clicks confirm
6. **Soft close:** After bidding, waits 2 seconds and reloads to check if auction extended
7. **Cancellation:** `cancelled` boolean flag checked each loop iteration in both phases

**Status callback pattern:**
```python
# In main.py when creating a snipe:
async def update_status(j: SnipeJob):
    with Session(engine) as s:
        db_snipe = s.get(Snipe, snipe.id)
        if db_snipe:
            db_snipe.status = j.status
            s.commit()

asyncio.create_task(job.run(on_status_change=update_status))
```

---

### `backend/hibid_api.py` - Direct HiBid GraphQL API

Places bids and registers for auctions via HiBid's GraphQL endpoint without Playwright. Uses the `sessionId` JWT from saved cookies.

**Key functions:**

```python
def get_auth_token() -> str | None:
    """Read sessionId JWT from data/hibid_cookies.json."""

async def place_bid_direct(lot_id, bid_amount, lot_url=None) -> dict:
    """Fire LotBid mutation via httpx.
    If RegisterFirst, auto-registers and retries once.
    Returns {success, status, message, suggested_bid}."""

async def _get_auction_id(lot_url) -> int | None:
    """Fetch lot page HTML, extract auction ID from Apollo cache."""

async def _register_for_auction(token, auction_id) -> bool:
    """Fire RegisterBuyer mutation to register as bidder."""

async def _get_payment_method_id(token) -> int | None:
    """Fetch first payment method ID on file via BuyerPayInfo query."""
```

**GraphQL mutations used:**
- `LotBid` — place a bid on a lot
- `RegisterBuyer` — register as bidder for an auction
- `BuyerPayInfo` — query for saved payment methods

**Headers include:** Bearer auth, User-Agent (Chrome), Origin/Referer (hibid.com), `__cf_bm` cookie for Cloudflare.

---

### `backend/hibid_scraper.py` - Lot Page Scraping

**Singleton browser pattern:**
```python
_browser = None  # Module-level singleton

async def get_browser():
    global _browser
    if _browser and _browser.contexts:
        return _browser
    pw = await async_playwright().start()
    _browser = await pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return _browser
```

**Lot scraping** uses flexible CSS selectors with wildcards to handle HiBid's varying class names:
```python
# These adapt to HiBid's CSS class naming patterns:
title = await _safe_text(page, "[class*='lot-title'], h1, .title")
current_bid = await _safe_text(page, "[class*='current-bid'], [class*='currentBid']")
increment = await _safe_text(page, "[class*='increment'], [class*='bid-increment']")
thumbnail = await page.query_selector("[class*='lot-image'] img, .gallery img")
```

**LotDetails dataclass** - Structured return from scraper:
```python
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
```

---

### `backend/hibid_auth.py` - Login Flow

Multi-strategy selector approach for handling HiBid's login page:

```python
async def login_if_needed(page):
    # 1. Check if already logged in
    logged_in = await page.query_selector("[class*='user-menu'], [class*='account'], .logged-in")
    if logged_in:
        return True

    # 2. Find and click login button
    login_btn = await page.query_selector("a[href*='login'], button:has-text('Login'), ...")

    # 3. Fill credentials from env vars
    await email_input.fill(HIBID_EMAIL)
    await password_input.fill(HIBID_PASSWORD)

    # 4. Submit and verify
    await submit_btn.click()
    # Wait and check for logged-in indicators
```

---

### `backend/discord_notify.py` - Notifications

**Embed builder pattern:**
```python
def _embed(title, color, fields, url=""):
    return {"embeds": [{
        "title": title,
        "url": url,
        "color": color,
        "fields": [{"name": f[0], "value": f[1], "inline": True} for f in fields],
    }]}

# Colors: 0x00FF00 (green/won), 0xFF0000 (red/lost), 0xFFA500 (orange/capped)
```

**Send function** - Async POST to Discord webhook, silent fail if not configured:
```python
async def send_notification(payload):
    if not DISCORD_WEBHOOK_URL:
        return  # Webhook not configured, skip silently
    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10.0)
```

---

### `backend/main.py` - FastAPI Application

**Lifecycle management:**
```python
@asynccontextmanager
async def lifespan(app):
    init_app_db()              # Create tables on startup
    await _resume_active_snipes()  # Recreate jobs for active snipes
    yield
    for job in active_snipes.values():
        job.cancel()           # Cancel all snipes on shutdown
```

**Resume on restart:**
```python
async def _resume_active_snipes():
    """Query DB for scheduled/watching/bidding snipes, recreate SnipeJobs."""
    # For each active snipe: create SnipeJob with end_time, start task
```

**In-memory snipe tracking:**
```python
active_snipes: dict[int, SnipeJob] = {}  # snipe_id -> SnipeJob
```

This dict lives in the process. On container restart, `_resume_active_snipes()` repopulates it from the DB.

**Pydantic request models:**
```python
class ManualBidRequest(BaseModel):
    bid_amount: float

class SnipeUpdate(BaseModel):
    max_cap: float | None = None
```

**Static file serving:**
```python
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
```

---

### `frontend/index.html` - Single-Page App

**One file, three sections:** CSS (<style>), HTML (body), JavaScript (<script>).

**CSS architecture:**
- CSS custom properties (variables) for theming in `:root`
- Dark theme: `--bg: #0f0f1a`, `--bg-card: #1a1a2e`
- Responsive: `@media (max-width: 600px)` breakpoint
- Component classes: `.card`, `.btn`, `.badge`, `.form-group`, `.price-grid`

**Tab system:**
```javascript
// Click handler swaps active class between tabs
$$('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        $$('.tab-btn').forEach(b => b.classList.remove('active'));
        $$('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        $(`#tab-${btn.dataset.tab}`).classList.add('active');
    });
});
```

**API helper:**
```javascript
async function api(url, opts = {}) {
    const res = await fetch(url, {
        headers: {'Content-Type': 'application/json'},
        ...opts,
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}
```

**XSS protection:**
```javascript
function esc(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;  // Safe: textContent doesn't parse HTML
    return d.innerHTML;   // Returns escaped version
}
```

**Auto-refresh for snipe queue:**
```javascript
let snipeRefreshTimer = null;

function startSnipeRefresh() {
    loadSnipes();
    snipeRefreshTimer = setInterval(loadSnipes, 5000);  // Every 5 seconds
}

function stopSnipeRefresh() {
    clearInterval(snipeRefreshTimer);
}
```

---

## Testing Strategy

Tests use pytest + pytest-asyncio. Each test file focuses on one module.

**Calculator tests** - Pure function testing, no mocks needed:
```python
def test_basic_true_cost():
    result = calculate_true_cost(100, 15)
    assert result["total"] == 129.95
```

**Bid logic tests** - Pure function testing:
```python
def test_should_bid_under_cap():
    assert should_bid(50, 100, 5) == True

def test_should_not_bid_over_cap():
    assert should_bid(95, 100, 10) == False
```

**API tests** - Use FastAPI TestClient:
```python
from fastapi.testclient import TestClient
client = TestClient(app)

def test_health():
    resp = client.get("/api/health")
    assert resp.json() == {"status": "ok"}
```

**Run tests:**
```bash
cd /home/htpc/hibid-sniper
python -m pytest tests/ -v
```

---

## Error Handling Patterns

| Module | Strategy |
|--------|----------|
| calculator.py | None needed - pure math, validated inputs |
| ebay.py | Try/except with logging, falls back to empty results |
| hibid_scraper.py | `_safe_text()` wraps all selectors in try/except returning None |
| hibid_auth.py | Returns bool success/failure, logs errors |
| sniper.py | Outer try/except in `run()`, sets status on failure |
| discord_notify.py | Silent failure if webhook not configured or request fails |
| main.py | HTTPException for 404s, try/except in endpoints |

---

## Key Design Decisions

1. **Single HTML file for frontend** - No build step, no npm, no framework. Works offline. Easy to modify.

2. **Startpage as eBay proxy** - eBay blocks all direct server requests. Startpage respects privacy and doesn't block automated queries (yet).

3. **Persistent browser context** - Saves HiBid cookies between container restarts. Reduces login frequency and bot detection risk.

4. **In-memory snipe tracking with DB resume** - Simple dict instead of Redis/message queue. On restart, recreated from DB. Sufficient for 2-5 simultaneous snipes.

5. **SQLite over Postgres** - Single-user local app. No concurrent write concerns. Zero configuration. File-based backup.

6. **Minimum increment bidding** - Never reveals max cap to competitors. Optimal strategy for soft-close auctions where you can re-bid.

7. **Ontario HST hardcoded** - `0.13` constant in calculator. If used outside Ontario, this needs to become configurable.

8. **Two-phase scheduling** - Auctions ending hours/days away don't need a browser open. Phase 1 sleeps with zero resource usage; Phase 2 activates 5 minutes before end.

9. **Direct GraphQL for manual bids** - Uses httpx + sessionId JWT instead of Playwright. Faster, lighter, no browser overhead. Playwright reserved for automated sniping where page state monitoring is needed.

10. **Budget counts won only** - Active bids don't reduce remaining budget. Prevents the budget from being "locked up" by pending snipes that may not win. Separate exposure bar shows potential worst case.
