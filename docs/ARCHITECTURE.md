# HiBid Sniper - Architecture

## System Overview

Local web application for analyzing online auction deals on HiBid.com and automatically placing last-second bids (sniping). Core features: true cost calculator, eBay price comparison via Startpage proxy, Playwright-powered bid automation, direct GraphQL manual bidding, and smart two-phase scheduling.

**Deployment:** Docker container on `localhost:8199`
**Stack:** Python 3.12 / FastAPI / SQLite / Playwright / httpx / Vanilla JS SPA

```
                    +-----------------+
                    |   Browser (UI)  |
                    |  Single-Page App|
                    +--------+--------+
                             |
                         HTTP/REST
                             |
                    +--------v--------+
                    |  FastAPI Server  |  Port 8199
                    |  (backend/main) |
                    +--+-----+-----+--+
                       |     |     |
            +----------+     |     +----------+
            |                |                |
   +--------v--------+  +---v-----------+  +-v-----------------+
   |   SQLite (ORM)  |  | hibid_api.py  |  | Playwright Browser|
   | auction_houses   |  | (httpx/GraphQL)|  | (Chromium headless)|
   | snipes, bid_logs |  +---+-----------+  +----+----------+---+
   | deal_checks      |      |                   |          |
   | settings         |      |       +-----------+    +-----+------+
   +--------+---------+      |       |                |            |
            |          +------v------+v-+   +---------v---+        |
     ./data/hibid_sniper.db  | HiBid.com   |   | Startpage   |    |
                     | (GraphQL+scrape) |   | (eBay proxy)|        |
                     +------------------+   +-------------+        |
                                                          +--------v--------+
                                                          | Discord Webhook |
                                                          | (notifications) |
                                                          +-----------------+
```

## Request Flows

### Deal Analysis Flow

```
User enters: item name + bid price + auction house
                    |
                    v
POST /api/deal-check?item_name=X&bid_price=Y&auction_house_id=Z
                    |
                    v
1. Look up auction house premium_pct from DB
2. calculate_true_cost(bid, premium_pct)
   - premium = bid * (premium_pct / 100)
   - subtotal = bid + premium
   - tax = subtotal * 0.13  (Ontario HST)
   - total = subtotal + tax
                    |
                    v
3. search_ebay_via_startpage(item_name)
   - GET startpage.com/sp/search?query="ebay.ca {item} price"
   - GET startpage.com/sp/search?query="ebay.ca {item} sold completed price"
   - Parse prices from HTML with regex
                    |
                    v
4. get_verdict(true_cost, ebay_avg_sold)
   - cost <= 0.85 * avg  =>  "good_deal"
   - cost <= 1.10 * avg  =>  "fair"
   - cost >  1.10 * avg  =>  "overpriced"
   - no data             =>  "unknown"
                    |
                    v
5. Save DealCheck record to SQLite
6. Return JSON: {cost breakdown, ebay prices, verdict, search URLs}
```

### Snipe Automation Flow (Two-Phase)

```
User queues snipe (bookmarklet or manual)
                    |
                    v
POST /api/snipes/from-browser (or POST /api/snipes)
                    |
                    v
1. Create Snipe DB record
   - If end_time provided: status = "scheduled"
   - Otherwise: status = "watching"
2. Create SnipeJob(end_time=...) in-memory
3. asyncio.create_task(job.run())
                    |
                    v
 PHASE 1: SLEEP (if end_time provided)
   status = "scheduled"
   No browser opened — zero resource usage
   Sleep in 60s chunks, check cancelled flag each iteration
   Wake up at T-5 minutes (WAKE_BEFORE_SECONDS = 300)
                    |
                    v
 PHASE 2: ACTIVE MONITORING
   status = "watching"
   Open Playwright browser, navigate to lot page
SnipeJob._phase_active() loop:
   +---> _get_auction_state(page)
   |     - Read current_price, increment, seconds_left
   |
   |     if ended:
   |       -> status = "won" or "lost"
   |       -> send Discord notification
   |       -> STOP
   |
   |     if next_bid > max_cap:
   |       -> status = "capped_out"
   |       -> send Discord notification
   |       -> STOP
   |
   |     if seconds_left > 3:
   |       -> sleep min(seconds_left - 3, 5)
   |       -> CONTINUE
   |
   |     if seconds_left <= 3:  <-- SNIPE WINDOW
   |       -> status = "bidding"
   |       -> bid_amount = current_price + increment
   |       -> _place_bid(page, bid_amount)
   |       -> sleep 2s (soft close check)
   |       -> reload page, recheck
   +-------+
```

### Bookmarklet Flow (from HiBid page)

```
User on HiBid lot page, clicks "Snipe This Lot" bookmarklet
                    |
                    v
1. Bookmarklet loader injects bookmarklet.js from app server
2. JS scrapes lot data from live DOM:
   - h1 -> title, .lot-high-bid -> price
   - .lot-bid-button -> next bid/increment
   - img -> thumbnail, URL -> lot_id
                    |
                    v
3. Fetch GET /api/auction-houses (populate dropdown)
4. User clicks "Check Market Value"
                    |
                    v
5. Fetch GET /api/search-ebay?query={title}
   - Shows eBay low/avg/high, listings
   - Shows cost table at various bid points
   - If no results: user types market value manually
                    |
                    v
6. User enters max cap, clicks "Queue Snipe"
7. Fetch POST /api/snipes/from-browser
   - Creates Snipe record, starts SnipeJob
   - No scraping needed (bookmarklet already has the data)
```

### Manual Bid Flow (Direct GraphQL)

```
User types bid amount in snipe row, clicks "Bid"
                    |
                    v
POST /api/snipes/{id}/bid  { "bid_amount": 25.00 }
                    |
                    v
1. Look up snipe from DB (get lot_id, lot_url)
2. Validate: bid > 0, bid fits within budget remaining
3. Call place_bid_direct(lot_id, bid_amount, lot_url)
   - Read sessionId JWT from data/hibid_cookies.json
   - Fire LotBid GraphQL mutation via httpx
   - If RegisterFirst: auto-register and retry once
4. Log result to bid_logs table
5. Update snipe.our_last_bid / current_bid in DB
6. Return {success, status, message, suggested_bid}
```

### Resume on Restart

```
Container starts -> FastAPI lifespan startup
                    |
                    v
_resume_active_snipes():
1. Query DB for snipes with status in (scheduled, watching, bidding)
2. For each: create SnipeJob, pass end_time if present
3. asyncio.create_task(job.run())
   - Scheduled snipes resume Phase 1 sleep
   - Watching/bidding snipes reopen browser in Phase 2
```

### Cookie-Based Auth Flow

```
User logs into HiBid in Chrome normally
                    |
                    v
Cookie Editor extension -> Export JSON
                    |
                    v
Paste into Settings tab -> POST /api/cookies
                    |
                    v
1. Filter to HiBid domain cookies
2. Save to data/hibid_cookies.json
3. Inject into running Playwright browser context
4. Snipe bot can now place bids as the user
```

### Data Model Relationships

```
auction_houses
  id              INTEGER PK
  name            VARCHAR NOT NULL
  premium_pct     FLOAT NOT NULL
  created_at      DATETIME DEFAULT NOW
       |
       |--- 1:M ---> snipes
       |               id, lot_url, lot_title, lot_id, thumbnail_url,
       |               max_cap, current_bid, increment, our_last_bid,
       |               status, end_time, auction_house_id (FK),
       |               created_at, updated_at
       |
       |--- 1:M ---> deal_checks
                       id, item_name, bid_price, true_cost,
                       ebay_avg_sold, ebay_low, ebay_high,
                       ebay_results (JSON text), amazon_search_url,
                       verdict, auction_house_id (FK), created_at
```

## Module Dependency Graph

```
main.py
  |-- db.py (get_engine, init_db)
  |-- models.py (AuctionHouse, Snipe, DealCheck, BidLog, Settings)
  |-- calculator.py (calculate_true_cost, get_verdict)
  |-- ebay.py (search_ebay, build_*_url)
  |-- hibid_api.py (place_bid_direct, get_auth_token)
  |-- sniper.py (SnipeJob)
  |     |-- hibid_scraper.py (get_browser, scrape_lot)
  |     |-- hibid_auth.py (login_if_needed)
  |     |-- calculator.py
  |     |-- discord_notify.py (send_notification, format_*)
  |-- hibid_scraper.py (scrape_lot)
```

## Concurrency Model

- **FastAPI** runs on a single uvicorn process with asyncio event loop
- **Snipe jobs** run as `asyncio.Task`s - multiple snipes execute concurrently on the same event loop
- **In-memory state**: `active_snipes: dict[int, SnipeJob]` tracks running jobs
- **Database sessions**: Short-lived SQLAlchemy sessions per request (no connection pooling needed for SQLite)
- **Browser instances**: Playwright persistent context shared across snipes (singleton pattern)
- **Resume on restart**: Lifespan startup queries DB for active snipes and recreates SnipeJobs
- **Graceful shutdown**: Lifespan handler cancels all active snipes on app shutdown

## Docker Container Layout

```
/app/
  backend/           # Python backend modules
  frontend/          # Static HTML/CSS/JS
  data/              # Mounted volume -> SQLite DB
  browser_profile/   # Mounted volume -> Playwright cookies/sessions
  requirements.txt
  Dockerfile
```

**Ports:** 8199 (HTTP only, no TLS)
**Volumes:**
- `./data:/app/data` - Database persistence across container restarts
- `./browser_profile:/app/browser_profile` - HiBid login session persistence
