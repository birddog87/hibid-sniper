# Changelog

## [Unreleased] - 2026-04-03

### Frontend UI Overhaul (Impeccable Audit)
- **Typography:** Added Space Grotesk heading font, bumped base from 15px to 16px, established 5-step type scale, set body line-height to 1.6
- **Color:** Replaced AI-purple accent (#6c63ff) with amber/gold (#d4a017), warmed backgrounds/text, replaced Material Design stock colors with natural tones
- **Responsive:** Added 768px tablet breakpoint, snipe queue table converts to card layout on mobile, 44px touch targets, tab scroll fade indicator
- **Mobile tabs:** Shortened labels on phones (Deal Analyzer → Deals, Snipe Queue → Snipes, etc.) so all 7 tabs fit without scrolling on S25 Ultra
- **Accessibility:** Full ARIA tab pattern (roles, states, keyboard nav with arrow keys), progressbar roles on budget bars, focus-visible indicators, skip nav link, semantic HTML (`<header>`, `<main>`), `for`/`id` on all form labels, `aria-sort` on sortable table headers
- **Alerts → Toasts:** Replaced all 26 `alert()` calls with non-blocking toast notifications, all 4 `confirm()` calls with styled inline confirmation dialogs
- **Loading states:** Added spinners to Snipe Queue, History, Activity Log, and Watchlist tabs while data loads
- **Badge system:** All badge backgrounds now use `color-mix()` referencing CSS variables instead of hardcoded rgba values
- **URL sanitization:** `safeUrl()` helper validates http/https protocol on all API-sourced URLs (dashboard + bookmarklet)
- **Reduced motion:** `prefers-reduced-motion` media query disables all animations
- **Polling optimization:** Budget/snipe refresh pauses when browser tab is hidden (`visibilitychange` API)
- **Error messages:** API errors now parse JSON `detail` field instead of showing raw `{"detail":"..."}` blobs
- **Watchlist NEW badge:** Scan results show amber "NEW" badge on items found since last scan, with per-group count
- **Default sort:** Snipe queue defaults to active-first then ending-soonest, so old capped-out snipes sink to bottom
- **Capped out / auth_failed recovery:** Resume and Cancel buttons now show on capped_out and auth_failed snipes, backend accepts resume from these states
- **Desktop table fix:** Snipe table has 850px min-width (reset on mobile) so action buttons don't clip; action button flex-wrap prevents overflow

### Sniper Timing Fixes
- **Wake-before increased:** 5 min → 15 min before estimated end time (more margin for inaccurate bookmarklet estimates)
- **Live end_time extraction:** Bot reads real end time from HiBid page on wake via `_extract_end_time_from_page()`, replacing bookmarklet estimate
- **Double-read validation:** Extracts end time twice with a page reload in between; if reads disagree by >30s (wrong-lot data), uses the later one
- **Timezone fix:** Bookmarklet and sniper now prefer `timeLeftSeconds` (timezone-immune countdown) over absolute time strings — fixes HiBid mislabeling EDT as EST (1-hour offset)
- **Wall-clock-only countdown:** `seconds_left` is computed purely from `end_time - now()`. Frozen page timer is ignored entirely for bid timing decisions. Page state is only used for price/ended detection.
- **Snipe window:** Changed from T-3 seconds to T-30 seconds for more margin on timing errors and bid retries
- **GraphQL bid timeout:** 15s `asyncio.wait_for` + 10s `AbortController` inside JS to prevent silent hangs. On timeout, page reloads automatically.

### Authentication Fixes
- **CRITICAL: Fixed `inject_cookies()` crash** — was calling `_browser.is_connected()` on `BrowserContext` (which has no such method), causing `AttributeError` silently caught by callers. Cookie imports via Settings tab and auth recovery never actually reached the running browser. Now uses `_browser_alive()` (the correct check).
- **Auth recovery on startup:** If token validation fails when bot wakes, reloads cookies from `data/hibid_cookies.json`, injects into browser, re-extracts token, and re-validates before giving up.
- **Auth failure detection during bids:** GraphQL bid responses now check HTTP 401/403 and auth-related error messages. If detected mid-bid, auto-refreshes cookies and retries instead of silently failing.
- **Reusable `_refresh_auth()` method:** Single function for cookie reload → inject → re-validate, used both at startup and mid-bid.

### Session Keepalive & JWT Management
- **Session keepalive task:** Background loop pings HiBid every 20 minutes (only when active snipes exist). Launches the browser if needed — doesn't skip when snipes are sleeping.
- **Cookie auto-save:** After each keepalive ping, captures all HiBid cookies from the browser context and writes them back to `data/hibid_cookies.json`. If HiBid issues a refreshed JWT on page visit, it's automatically captured — potentially extending the 7-day session indefinitely.
- **JWT expiry decoding:** New `_decode_jwt_exp()` helper decodes the JWT payload's `exp` claim without a library. Used by both the keepalive (logging) and the cookie status API.
- **JWT expiry banner in UI:** Amber warning when session expires in <24 hours, red danger at <4 hours, "EXPIRED" when dead. Checks every 60 seconds. Shows above the budget bar on all tabs.
- **`/api/cookies/status` enhanced:** Now returns `jwt_expires_at` (Unix timestamp) and `jwt_expires_in_hours` (float) decoded from the actual JWT, not just cookie metadata.

### Pre-registration at Queue Time
- **Immediate registration on bookmarklet queue:** When a snipe is created via `POST /api/snipes/from-browser`, the bot immediately registers for that auction via GraphQL (`RegisterBuyer` mutation) using httpx — no Playwright needed.
- Fire-and-forget (`asyncio.create_task`), non-blocking, non-fatal. If it fails (expired auth, network issue), the bot retries registration on wake via the existing `_ensure_registered()` path.
- Uses existing functions from `hibid_api.py`: `get_auth_token()`, `_get_auction_id()`, `_register_for_auction()`.

### HTTP-based Timer Cross-check
- **New `get_lot_status_via_html()` in `hibid_api.py`:** Fetches the lot page HTML via httpx and extracts `timeLeftSeconds`, `currentBidAmount`, `bidCount`, and ended status from the Apollo SSR data. No Playwright, no frozen timer.
- **30-second cross-check in poll loop:** During active monitoring, every 30 seconds the bot fetches the lot page via HTTP and compares `timeLeftSeconds` with the wall clock. If they disagree by >60 seconds, corrects `end_time`. Also detects ended state and price updates independently of the page DOM.
- This catches wrong-lot `timeLeftSeconds` contamination that the initial double-read validation might miss, and provides a second independent timer source throughout the auction.

### Previous (2026-03-31)

### Bidding Reliability
- Fixed a self-outbidding soft-close bug where HiBid could return `NO_BID` + `PreviousMaxBid` but the bot kept rebidding anyway.
- Normalized `bidStatus` / `bidMessage` values from the GraphQL bid API before branching, so whitespace/casing variations still resolve correctly.
- Added a committed-max guard so the bot holds when the visible current price is still at or below the max bid we already have on file.
- Updated projected exposure math to replace the known committed max instead of loosely relying on the visible current price.

### Tests / Ops
- Added sniper regression tests covering `PreviousMaxBid` normalization and self-outbidding prevention.
- Documented the Mar 31 live bidding failure and fix in `BOT_BID_PROBLEM.md`.
- Rebuilt the Docker service to apply the live fix (`docker compose up -d --build`).

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
