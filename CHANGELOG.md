# Changelog

## [0.4.1] - 2026-02-27

### Safety Hardening (Bid Guardrails)
- Fixed ended-state detection so `"Closed"` / `"Ended"` pages resolve correctly even when no numeric countdown is present.
- Added "already winning" protection so the bot does not bid against itself during the snipe window.
- Added time-parse safety (`-1` unknown) so parse failures do not trigger accidental early bids.
- Added cleanup of completed jobs from `active_snipes` (`won` / `lost` / `capped_out`) to avoid stale in-memory jobs.
- Added budget validation when editing max cap via `PUT /api/snipes/{snipe_id}`.
- Added stricter manual-bid safety checks at bid time:
  - must be within snipe cap
  - must be within single-snipe cap
  - must be within remaining budget
  - must not push projected exposure above global cap
- Added optional emergency hard stop: `EMERGENCY_BID_HARD_MAX` (set `0` to disable).
- Added settings sanity check: `max_single_snipe_cap` cannot exceed `global_spend_cap` when cap > 0.

### Security / Operations
- Tightened CORS from wildcard to an explicit allowlist (HiBid + local app origins).
- Removed dangerous exploratory scripts (`test_bid.py`, `test_graphql_bid.py`) that could trigger live bidding.
- Sanitized `docs/SECURITY.md` examples to remove real-looking credential values.

### Tests / Docs
- Added unit tests for new bid safety checks (`tests/test_budget_safety.py`).
- Extended sniper tests for projected exposure helper (`tests/test_sniper.py`).
- Documented `EMERGENCY_BID_HARD_MAX` in `.env.example` and deployment docs.

## [0.4.0] - 2026-02-27

### Smart Snipe Scheduling
- **Two-phase snipe execution** — Phase 1 sleeps with zero resource usage (no browser), Phase 2 opens Playwright at T-5 minutes
- Bookmarklet parses auction countdown and sends `end_time` to backend
- Snipes start in "scheduled" status with purple badge, transition to "watching" when active phase begins
- **Resume on restart** — container startup recreates SnipeJobs for all scheduled/watching/bidding snipes from DB

### Direct GraphQL Bidding (Manual Bid)
- **New:** "Bid Now" inline input on each snipe row — type an amount and place a bid instantly
- Uses HiBid's GraphQL `LotBid` mutation via httpx (no Playwright needed)
- Auth via `sessionId` JWT from saved cookies file
- **Auto-registration** — if bid returns `RegisterFirst`, automatically registers for the auction and retries
- Shows WINNING/OUTBID/error result as inline flash, auto-fills suggested next bid amount
- Table refresh paused while typing in bid input (no more disappearing input)

### Winning Status Indicator
- Snipe rows show "YOU'RE WINNING" (green) or "OUTBID" (red) based on `our_last_bid` vs `current_bid`
- `ACCEPTED` bid status now recognized as successful (in addition to WINNING/OUTBID)

### Inline Max Cap Editing
- Click any max cap value to edit it inline — saves on Enter/blur, Escape cancels
- Updates both DB and in-memory SnipeJob

### Budget Model Change
- Budget bar now only counts **actual won auctions** as "spent" (was counting theoretical max_cap exposure)
- **New:** Second "If you win" exposure bar showing potential spend based on current active bids
- Bar text changed from "used" to "spent"

### Backend
- `backend/hibid_api.py` — new module for direct HiBid GraphQL API calls via httpx
- `PUT /api/snipes/{snipe_id}` — edit max cap on existing snipes
- `POST /api/snipes/{snipe_id}/bid` — place manual bid via GraphQL API
- `GET /api/snipes` now returns `end_time`, `our_last_bid`, `increment`
- `GET /api/budget` now returns `exposure`, `exposure_total`, `exposure_pct`
- Snipe status "scheduled" added to active statuses for cancel-all and budget

### Frontend
- Purple "Scheduled" badge and "Ends In" countdown column
- Bid input + Bid button per snipe row
- Exposure bar below budget bar
- Click-to-edit max cap cells

## [0.3.0] - 2026-02-27

### Chrome Bookmarklet
- **New:** "Snipe This Lot" bookmarklet — click on any HiBid lot page to analyze and snipe
- Two-step flow: analyze market value first, then queue snipe with informed cap
- Fetches eBay prices, shows cost table at different bid points
- **Manual market value input** — when eBay finds nothing, type what you found on Amazon/Kijiji/etc
- Live true cost preview as you type your max cap, with instant verdict
- Auction house dropdown pulled live from the app (no ID memorizing)
- Loaded as external JS from app server — no bookmarklet size limits, always up to date

### Cookie-Based Auth
- **New:** Cookie import via Settings tab — paste cookies from Cookie Editor extension
- Cookies injected into Playwright browser context for bid placement
- Cookie status indicator in the UI
- Bypasses Cloudflare Turnstile CAPTCHA that blocks automated login

### Real HiBid Selectors
- Replaced all guessed CSS selectors with real ones from live HiBid pages
- `.lot-high-bid` for current bid, `.lot-bid-button` for bid action
- `.lot-time-left` for countdown, `.login-link` for auth state
- Bid mechanism fixed: no input field, just click the bid button (contains next bid amount)
- Added winning bidder detection via page text search

### Backend
- `POST /api/snipes/from-browser` — create snipes from bookmarklet data (no scraping needed)
- `POST /api/cookies` — import HiBid session cookies
- `GET /api/cookies/status` — check if cookies are loaded
- CORS middleware enabled for cross-origin bookmarklet requests

### Frontend
- **New:** Setup tab with bookmarklet instructions and cookie import
- All existing tabs unchanged

## [0.2.0] - 2026-02-26

### eBay Price Discovery (Startpage Proxy)
- eBay blocks all server-side scraping — Startpage.com used as search proxy
- Parallel async queries (active + sold) via `asyncio.gather` for speed (<1s)
- eBay-only URL filtering (no Reddit/forum results polluting data)
- Junk keyword filtering (parts only, broken, cases, cables, etc.)
- Deduplication by URL across both queries
- Price range: $1–$50,000 to filter garbage
- CSS artifact cleanup from Startpage HTML

### Deal Analyzer Improvements
- Listing drill-down: click to expand individual eBay results with links
- Combined "eBay Market Prices" label when sold data unavailable
- Savings display vs eBay average
- Search buttons: eBay Active, eBay Sold, Amazon, Kijiji, FB Marketplace

### Documentation
- `docs/ARCHITECTURE.md` — system diagram, data flow, module dependencies
- `docs/API-REFERENCE.md` — every REST endpoint with examples
- `docs/SECURITY.md` — threat model, credential handling
- `docs/BUSINESS-LOGIC.md` — true cost formula, verdict thresholds, bid strategy
- `docs/CODE-GUIDE.md` — module-by-module walkthrough
- `docs/DEPLOYMENT.md` — Docker commands, env vars, troubleshooting

## [0.1.0] - 2026-02-25

### Initial Release
- True cost calculator (bid + buyer's premium + Ontario HST)
- Deal verdict logic (good deal / fair / overpriced / unknown)
- Snipe bot engine with minimum increment bidding and soft close handling
- HiBid lot page scraper via Playwright
- HiBid login automation
- Discord webhook notifications (won/lost/capped)
- FastAPI REST API with all CRUD endpoints
- Single-file dark-themed web dashboard (HTML + CSS + JS)
- Auction house management with configurable premiums
- SQLite database with auto-migration
- Docker deployment with persistent browser profile
