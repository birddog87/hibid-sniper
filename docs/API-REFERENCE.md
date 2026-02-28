# HiBid Sniper - API Reference

Base URL: `http://localhost:8199` (or your `APP_URL`)

All endpoints return JSON. No authentication required (local network only).

---

## Health Check

### `GET /api/health`
Returns server status.

**Response:**
```json
{"status": "ok"}
```

---

## Calculator

### `POST /api/calculate`
Calculate true cost of a bid (premium + HST).

**Request Body:**
```json
{
  "bid_price": 100.00,
  "premium_pct": 15.0
}
```

**Response:**
```json
{
  "bid_price": 100.0,
  "premium_pct": 15.0,
  "premium_amount": 15.0,
  "subtotal": 115.0,
  "tax_rate": 0.13,
  "tax_amount": 14.95,
  "total": 129.95
}
```

---

## Deal Analyzer

### `POST /api/deal-check`
Full deal analysis: cost calculation + eBay price comparison + verdict.

**Query Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `item_name` | string | yes | Item to search for on eBay |
| `bid_price` | float | yes | Your proposed bid amount |
| `auction_house_id` | int | yes | ID of saved auction house |

**Response:**
```json
{
  "deal_id": 9,
  "cost": {
    "bid_price": 50.0,
    "premium_pct": 14.0,
    "premium_amount": 7.0,
    "subtotal": 57.0,
    "tax_rate": 0.13,
    "tax_amount": 7.41,
    "total": 64.41
  },
  "ebay": {
    "active": {
      "count": 2,
      "low": 61.62,
      "high": 115.56,
      "avg": 88.59,
      "listings": [
        {"title": "Logitech MX Master 3S...", "price": 61.62, "url": "https://ebay.ca/itm/..."}
      ],
      "search_url": "https://www.ebay.ca/sch/i.html?_nkw=..."
    },
    "sold": {
      "count": 3,
      "low": 61.62,
      "high": 116.06,
      "avg": 97.75,
      "listings": [...],
      "search_url": "https://www.ebay.ca/sch/i.html?_nkw=...&LH_Sold=1"
    },
    "amazon_url": "https://www.amazon.ca/s?k=...",
    "kijiji_url": "https://www.kijiji.ca/b-ontario/k0l9004?q=...",
    "fb_marketplace_url": "https://www.facebook.com/marketplace/burlington/search?query=..."
  },
  "verdict": "good_deal"
}
```

**Verdict Values:**
| Verdict | Meaning | Threshold |
|---------|---------|-----------|
| `good_deal` | Save 15%+ vs market | true_cost <= 0.85 * ebay_avg |
| `fair` | Within 10% of market | true_cost <= 1.10 * ebay_avg |
| `overpriced` | Paying 10%+ over market | true_cost > 1.10 * ebay_avg |
| `unknown` | No eBay pricing data | ebay_avg is null |

**Errors:**
- `404`: Auction house not found

---

## eBay Search

### `GET /api/search-ebay`
Search eBay prices via Startpage proxy (standalone, without deal analysis).

**Query Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Search terms |

**Response:** Same `ebay` structure as deal-check response.

---

## Auction Houses

### `GET /api/auction-houses`
List all saved auction houses.

**Response:**
```json
[
  {"id": 1, "name": "Burlington Auction Centre", "premium_pct": 15.0},
  {"id": 2, "name": "Walters Auctions", "premium_pct": 14.0}
]
```

### `POST /api/auction-houses`
Create a new auction house.

**Request Body:**
```json
{
  "name": "Burlington Auction Centre",
  "premium_pct": 15.0
}
```

**Response:**
```json
{"id": 1, "name": "Burlington Auction Centre", "premium_pct": 15.0}
```

### `DELETE /api/auction-houses/{house_id}`
Delete an auction house.

**Response:**
```json
{"ok": true}
```

**Errors:**
- `404`: Not found

---

## Snipes

### `GET /api/snipes`
List all active snipes (excludes cancelled).

**Response:**
```json
[
  {
    "id": 1,
    "lot_url": "https://hibid.com/lot/123456/...",
    "lot_title": "Milwaukee M18 Impact Driver",
    "max_cap": 100.0,
    "current_bid": 45.0,
    "our_last_bid": 45.0,
    "increment": 5.0,
    "status": "watching",
    "end_time": "2026-02-28T14:00:00",
    "thumbnail_url": "https://hibid.com/images/..."
  }
]
```

**Status Values:**
| Status | Meaning |
|--------|---------|
| `scheduled` | Sleeping until T-5 minutes (no browser open) |
| `watching` | Monitoring auction, waiting for snipe window |
| `bidding` | Currently placing bids in final seconds |
| `won` | Auction won |
| `lost` | Outbid, auction ended |
| `capped_out` | Next bid would exceed max cap |
| `cancelled` | Manually cancelled by user |

### `POST /api/snipes`
Queue a new snipe. Scrapes the lot page and starts monitoring.

**Request Body:**
```json
{
  "lot_url": "https://hibid.com/lot/123456/some-item",
  "max_cap": 100.0,
  "auction_house_id": 1
}
```

**Response:**
```json
{
  "id": 1,
  "lot_title": "Milwaukee M18 Impact Driver",
  "current_bid": 45.0,
  "increment": 5.0,
  "status": "watching"
}
```

**Errors:**
- `404`: Auction house not found

### `PUT /api/snipes/{snipe_id}`
Update an existing snipe (currently supports editing max cap).

**Request Body:**
```json
{
  "max_cap": 75.0
}
```

**Response:**
```json
{"ok": true}
```

**Errors:**
- `404`: Snipe not found

### `POST /api/snipes/{snipe_id}/bid`
Place a manual bid on a snipe's lot via HiBid's GraphQL API.

**Request Body:**
```json
{
  "bid_amount": 25.00
}
```

**Response (success):**
```json
{
  "success": true,
  "status": "WINNING",
  "message": "",
  "suggested_bid": 30.0
}
```

**Response (outbid):**
```json
{
  "success": true,
  "status": "OUTBID",
  "message": "",
  "suggested_bid": 30.0
}
```

**Errors:**
- `404`: Snipe not found
- `400`: Bid exceeds budget remaining
- `500`: No auth token (cookies not imported)

**Notes:**
- If the user isn't registered for the auction, auto-registers and retries once
- Updates `our_last_bid` and `current_bid` in DB on success
- Logs bid to `bid_logs` table

### `POST /api/snipes/{snipe_id}/cancel`
Cancel an active snipe.

**Response:**
```json
{"ok": true}
```

### `POST /api/snipes/from-browser`
Queue a snipe using data sent from the bookmarklet. No server-side scraping needed.

**Request Body:**
```json
{
  "lot_url": "https://hibid.com/lot/123456/some-item",
  "lot_title": "ErGear Dual Monitor Desk Mount",
  "lot_id": "123456",
  "current_bid": 0.0,
  "increment": 1.0,
  "thumbnail_url": "https://hibid.com/images/...",
  "max_cap": 50.0,
  "auction_house_id": 1,
  "end_time": "2026-02-28T14:00:00Z"
}
```

**Notes:**
- `end_time` is optional. If provided, the snipe starts in "scheduled" status and sleeps until T-5 minutes.
- The bookmarklet computes `end_time` from the lot page countdown timer.

**Response:**
```json
{
  "id": 3,
  "lot_title": "ErGear Dual Monitor Desk Mount",
  "current_bid": 0.0,
  "status": "scheduled"
}
```

---

## Cookies

### `POST /api/cookies`
Import HiBid session cookies for Playwright bid placement.

**Request Body:**
```json
{
  "cookies": [
    {"name": "session_id", "value": "abc123", "domain": ".hibid.com", "path": "/"},
    ...
  ]
}
```

**Response:**
```json
{"ok": true, "count": 12}
```

**Errors:**
- `400`: No HiBid cookies found in provided data

### `GET /api/cookies/status`
Check if HiBid cookies are loaded.

**Response:**
```json
{"has_cookies": true, "count": 12}
```

---

## Budget

### `GET /api/budget`
Get current budget status including spend and exposure.

**Response:**
```json
{
  "daily_budget": 500.0,
  "spent": 85.0,
  "remaining": 415.0,
  "pct": 17.0,
  "exposure": 53.0,
  "exposure_total": 138.0,
  "exposure_pct": 27.6
}
```

| Field | Meaning |
|-------|---------|
| `spent` | Sum of `current_bid` on won snipes today |
| `remaining` | `daily_budget - spent` |
| `pct` | Percentage of budget spent |
| `exposure` | Sum of `our_last_bid` or `current_bid` on active snipes |
| `exposure_total` | `spent + exposure` (potential total if all active bids win) |
| `exposure_pct` | `exposure_total / daily_budget * 100` |

---

## Settings

### `GET /api/settings`
Get current app settings.

**Response:**
```json
{
  "global_spend_cap": 500.0
}
```

### `PUT /api/settings`
Update app settings.

**Request Body:**
```json
{
  "global_spend_cap": 750.0
}
```

**Response:**
```json
{"ok": true}
```

---

## History

### `GET /api/history`
Retrieve past deal checks and completed snipes.

**Response:**
```json
{
  "deals": [
    {
      "id": 9,
      "item_name": "logitech mx master 3s",
      "bid_price": 50.0,
      "true_cost": 64.41,
      "ebay_avg_sold": 97.75,
      "verdict": "good_deal",
      "created_at": "2026-02-27 15:30:00"
    }
  ],
  "snipes": [
    {
      "id": 1,
      "lot_title": "Milwaukee M18 Impact Driver",
      "lot_url": "https://hibid.com/lot/123456/...",
      "max_cap": 100.0,
      "current_bid": 75.0,
      "status": "won",
      "created_at": "2026-02-27 14:00:00"
    }
  ]
}
```

**Limits:** 50 most recent records per category, ordered by date descending.

---

## Frontend

### `GET /`
Serves the single-page application (`frontend/index.html`).

### `GET /static/{path}`
Serves static files from the `frontend/` directory.
