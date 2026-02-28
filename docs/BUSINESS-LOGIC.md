# HiBid Sniper - Business Logic & Calculations

## True Cost Formula

When you win an auction bid, you don't just pay the hammer price. Auction houses add a **buyer's premium** on top, and then the government adds **sales tax** on the whole thing.

### The Math

```
INPUT:
  bid_price    = your winning bid (e.g., $100)
  premium_pct  = auction house buyer's premium (e.g., 15%)

CALCULATION:
  premium_amount = bid_price * (premium_pct / 100)
  subtotal       = bid_price + premium_amount
  tax_amount     = subtotal * 0.13              (Ontario HST, hardcoded)
  total          = subtotal + tax_amount

OUTPUT:
  All of the above in a dict, rounded to 2 decimal places
```

### Worked Example

```
Bid: $100.00  |  Premium: 15%  |  HST: 13%

  Premium:  $100.00 * 0.15 = $15.00
  Subtotal: $100.00 + $15.00 = $115.00
  HST:      $115.00 * 0.13 = $14.95
  TOTAL:    $115.00 + $14.95 = $129.95

You pay $129.95 for a $100 bid.
That's 29.95% more than the hammer price.
```

### Premium Ranges (Burlington, ON area)
| Auction House | Typical Premium |
|---------------|----------------|
| Low end | 14% |
| Average | 15% |
| High end | 18% |

### Implementation
**File:** `backend/calculator.py`
**Function:** `calculate_true_cost(bid_price: float, premium_pct: float) -> dict`
**Constant:** `HST_RATE = 0.13`

---

## Deal Verdict Logic

Compares your true cost against eBay market average to decide if the deal is worth it.

### Thresholds

```
                    0.85x               1.10x
  |--- GOOD DEAL ---|------ FAIR -------|--- OVERPRICED --->
  $0              $85.00             $110.00
                  (if eBay avg = $100)
```

| Verdict | Condition | Meaning |
|---------|-----------|---------|
| `good_deal` | true_cost <= 0.85 * ebay_avg | You're saving 15%+ vs market |
| `fair` | 0.85 * ebay_avg < true_cost <= 1.10 * ebay_avg | Within 10% of market |
| `overpriced` | true_cost > 1.10 * ebay_avg | Paying 10%+ more than market |
| `unknown` | ebay_avg is None | No price data to compare against |

### Priority
Uses **sold prices** first (actual completed transactions). Falls back to **active listing prices** if no sold data available.

```python
ebay_avg = ebay["sold"]["avg"] or ebay["active"]["avg"]
verdict = get_verdict(cost["total"], ebay_avg)
```

### Implementation
**File:** `backend/calculator.py`
**Function:** `get_verdict(true_cost: float, ebay_avg_sold: float | None) -> str`

---

## Snipe Bidding Strategy

The sniper uses a **minimum increment strategy** to win auctions at the lowest possible price without revealing your maximum willingness to pay.

### Core Principles

1. **Never bid your max upfront** - Other bidders can see the bid history. Bidding your max early invites incremental bidding wars.

2. **Bid minimum increment only** - If current bid is $50 and increment is $5, bid $55. Not $60, not your max of $100.

3. **Bid in the final 3 seconds** - This is the "snipe window." Other bidders have no time to respond.

4. **Handle soft close extensions** - HiBid extends auctions when bids come in the last minute. The sniper re-engages after each extension.

5. **Stop at your cap** - If next_bid (current + increment) would exceed your max, stop. Never overpay.

### Bid Decision Logic

```python
def should_bid(current_price, max_cap, increment):
    return current_price + increment <= max_cap

def next_bid_amount(current_price, increment):
    return current_price + increment
```

### Example Scenario

```
Your max cap: $100
Auction increment: $5

Time: 2:00pm  |  Current bid: $50  |  Status: watching (waiting)
Time: 2:55pm  |  Current bid: $70  |  Status: watching (2 min left)
Time: 2:59:57 |  Current bid: $70  |  Status: BIDDING -> bid $75
  (Soft close triggers - auction extends 2 min)
Time: 3:01:57 |  Current bid: $80  |  Status: BIDDING -> bid $85
  (Another bidder bid $80, soft close extends again)
Time: 3:03:57 |  Current bid: $85  |  Status: watching (we're winning)
Time: 3:04:00 |  Auction ends       |  Status: WON at $85

Your true cost at $85 with 15% premium + 13% HST: $110.16
You saved $15 vs your $100 cap (true cost would have been $129.95).
```

### Timing Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `SNIPE_WINDOW_SECONDS` | 3 | Place bid when <= 3 seconds remain |
| `POLL_INTERVAL` | 5 | Check auction state every 5 seconds |
| `SOFT_CLOSE_RECHECK` | 2 | Wait 2 seconds after bid to check for soft close |

### Stop Conditions

| Condition | Result Status | Action |
|-----------|---------------|--------|
| Auction ended, we're winning | `won` | Discord notification (green) |
| Auction ended, we lost | `lost` | Discord notification (red) |
| Next bid exceeds max cap | `capped_out` | Discord notification (orange) |
| User clicks cancel | `cancelled` | Job stopped, no notification |

### Two-Phase Scheduling

Snipes with a known `end_time` use a two-phase approach to minimize resource usage:

| Phase | Duration | Browser | CPU/RAM |
|-------|----------|---------|---------|
| Phase 1: Sleep | Until T-5 minutes | Not opened | Near zero |
| Phase 2: Active | Last 5 minutes | Playwright open, polling every 5s | Normal |

Phase 1 sleeps in 60-second chunks, checking for cancellation each iteration. This prevents opening dozens of browser tabs for auctions ending hours or days away.

### Implementation
**File:** `backend/sniper.py`
**Class:** `SnipeJob`
**Key methods:** `run()`, `_phase_sleep()`, `_phase_active()`, `_get_auction_state()`, `_place_bid()`, `cancel()`
**Constants:** `WAKE_BEFORE_SECONDS = 300`, `SNIPE_WINDOW_SECONDS = 3`, `POLL_INTERVAL = 5`

---

## Budget Model

### Global Spend Cap
A configurable daily budget (stored in `settings` table, default $500). Prevents the bot from spending more than intended.

### What Counts as "Spent"
Only **won auctions** count against the budget. Active snipes and bids do not reduce the remaining budget.

```
spent = sum(current_bid) for snipes WHERE status = 'won' AND created today
remaining = global_spend_cap - spent
```

### Exposure Bar
A separate "If you win" bar shows potential total spend:

```
exposure = sum(our_last_bid or current_bid) for active snipes (scheduled/watching/bidding)
exposure_total = spent + exposure
```

This gives the user visibility into worst-case spending without blocking new bids.

### Budget Validation
Manual bids are validated: `bid_amount <= remaining budget`. Automated snipe bids also check the budget before placing.

### Implementation
**File:** `backend/main.py`
**Function:** `get_budget_status()`
**Endpoint:** `GET /api/budget`

---

## Manual Bidding (Direct GraphQL)

Users can place bids from the UI without waiting for the snipe window. This uses HiBid's GraphQL API directly via httpx — no Playwright browser needed.

### Flow
1. User types amount in the "Bid Now" input on a snipe row
2. Backend fires `LotBid` mutation with the `sessionId` JWT from saved cookies
3. HiBid returns: `WINNING`, `OUTBID`, `ACCEPTED`, or `NO_BID`
4. Result shown inline; suggested next bid auto-filled

### Auto-Registration
If HiBid returns `NO_BID: RegisterFirst`, the system:
1. Fetches the lot page HTML to extract the auction ID from Apollo cache
2. Queries `BuyerPayInfo` for the payment method on file
3. Fires `RegisterBuyer` mutation to register for the auction
4. Retries the original bid

### Success Statuses
`WINNING`, `OUTBID`, and `ACCEPTED` are all treated as successful bids. `ACCEPTED` means the bid was placed but HiBid hasn't determined winner/outbid status yet.

### Implementation
**File:** `backend/hibid_api.py`
**Functions:** `place_bid_direct()`, `get_auth_token()`, `_register_for_auction()`, `_get_auction_id()`
**Endpoint:** `POST /api/snipes/{snipe_id}/bid`

---

## eBay Price Scraping via Startpage

eBay blocks all server-side requests (httpx, curl, Playwright all fail with challenge pages or crashes). Startpage.com is used as a privacy-respecting search proxy.

### How It Works

1. **Active listings query:** `"ebay.ca {item_name} price"`
2. **Sold listings query:** `"ebay.ca {item_name} sold completed price"`
3. Startpage returns Google-like search results that include eBay listing data
4. Regex parser extracts prices from result titles and descriptions

### Price Extraction

```
Input HTML (Startpage result):
  <p class="description css-...">
    Logitech MX Master 3S bluetooth Wireless Laser Mouse
    C $61.62. Buy It Now. +C $16.54 shipping.
  </p>

Regex: (?:C\s*\$|CA\$|CAD\s*\$?|\$)\s*([\d,]+\.?\d*)

Extracted: $61.62  (shipping prices filtered by position - first price taken)
```

### Price Filtering
- Minimum: $1.00 (filters out $0 and micro amounts)
- Maximum: $50,000 (filters out garbage/unrelated numbers)
- One price per search result (first match wins)

### Aggregation
For each category (active/sold):
```
count = number of prices found
low   = min(prices)
high  = max(prices)
avg   = round(sum(prices) / len(prices), 2)
```

### Fallback
If Startpage returns no results or is down:
- Returns empty price data (`count: 0, low/high/avg: null`)
- Search URL buttons still work (user can manually check)
- Verdict defaults to `"unknown"`

### Implementation
**File:** `backend/ebay.py`
**Function:** `search_ebay_via_startpage(query: str) -> dict`
**Helpers:** `_startpage_search()`, `_parse_startpage_prices()`

---

## Platform Search URLs

Generated for manual price checking. User clicks buttons that open in new tabs.

| Platform | URL Pattern | Scope |
|----------|-------------|-------|
| eBay Active | `ebay.ca/sch/i.html?_nkw={query}&_sop=15` | Canada, sorted by price low-high |
| eBay Sold | `ebay.ca/sch/i.html?_nkw={query}&LH_Complete=1&LH_Sold=1&_sop=15` | Canada, completed + sold only |
| Amazon | `amazon.ca/s?k={query}` | Canada |
| Kijiji | `kijiji.ca/b-ontario/k0l9004?q={query}` | Ontario |
| Facebook Marketplace | `facebook.com/marketplace/burlington/search?query={query}` | Burlington, ON area |

---

## Discord Notification Payloads

### Snipe Won (Green)
```json
{
  "embeds": [{
    "title": "WON: Milwaukee M18 Impact Driver",
    "url": "https://hibid.com/lot/123456/...",
    "color": 65280,
    "fields": [
      {"name": "Winning Bid", "value": "$85.00", "inline": true},
      {"name": "True Cost", "value": "$110.16", "inline": true}
    ]
  }]
}
```

### Snipe Lost (Red)
```json
{
  "embeds": [{
    "title": "LOST: Milwaukee M18 Impact Driver",
    "color": 16711680,
    "fields": [
      {"name": "Final Price", "value": "$125.00", "inline": true},
      {"name": "Your Cap", "value": "$100.00", "inline": true}
    ]
  }]
}
```

### Capped Out (Orange)
```json
{
  "embeds": [{
    "title": "CAPPED: Milwaukee M18 Impact Driver",
    "color": 16753920,
    "fields": [
      {"name": "Current Price", "value": "$98.00", "inline": true},
      {"name": "Your Cap", "value": "$100.00", "inline": true}
    ]
  }]
}
```

### Implementation
**File:** `backend/discord_notify.py`
**Functions:** `format_snipe_won()`, `format_snipe_lost()`, `format_snipe_capped()`, `send_notification()`
