# HiBid Deal Analyzer & Snipe Bot

## Overview
Local web app for analyzing HiBid auction deals and auto-sniping at the last moment.

## Three Core Features

### 1. True Cost Calculator
- Input: bid price + auction house (saved with buyer's premium %)
- Adds buyer's premium (14-16% depending on house)
- Adds Ontario HST (13%)
- Shows real total cost

Example: $100 bid + 15% premium + 13% HST = $129.95 actual cost

### 2. Price Comparison
- **eBay (automated)**: Free API - sold prices (range + links) and active listings (range + links)
- **Amazon**: Auto-generated search link (click to check yourself)
- Verdict: Good deal / Fair / Overpriced based on true cost vs eBay sold average

### 3. Snipe Bot
- Logs into HiBid via Playwright (persistent browser profile)
- Monitors auction countdown
- Bids current price + minimum increment in final moments
- Handles soft close: waits and rebids through extensions
- Never reveals your max - only increments minimally
- Stops when you win or hit your max cap
- Supports up to 5 simultaneous snipes
- Discord webhook notifications (win/loss/capped out)

## Tech Stack
- **Backend**: Python / FastAPI
- **Frontend**: Single-page HTML/CSS/JS
- **Browser Automation**: Playwright (headless Chrome)
- **Database**: SQLite
- **Price Data**: eBay Browse/Finding API (free)
- **Notifications**: Discord webhook
- **Deployment**: Docker on 192.168.1.121

## Web Dashboard Views

### Deal Analyzer
- Item name/description + bid price + auction house dropdown
- True cost breakdown
- eBay sold prices (low / avg / high + links)
- eBay active listings (low / avg / high + links)
- Amazon search link
- Deal verdict

### Snipe Queue
- List of queued auctions: thumbnail, title, current bid, your max cap, true cost at cap, time remaining
- Status per item: Watching / Bidding / Won / Lost / Capped Out
- Add snipe: paste HiBid URL + set max cap
- Cancel button

### Auction Houses
- Save frequent houses with their buyer's premium %
- e.g. "Burlington Auction Centre - 15%"

### History
- Past snipes and deal checks
- What you won, what you paid, market comparison

## Project Structure
```
/home/htpc/hibid-sniper/
├── backend/
│   ├── main.py              # FastAPI server
│   ├── calculator.py        # True cost math
│   ├── ebay.py              # eBay API price lookups
│   ├── sniper.py            # Playwright snipe bot logic
│   ├── discord_notify.py    # Discord webhook notifications
│   └── db.py                # SQLite for history/settings
├── frontend/
│   └── index.html           # Single page app
├── docker-compose.yml       # Containerized deployment
├── .env                     # Credentials (HiBid, Discord, eBay)
└── README.md
```

## One-Time Setup (all free)
1. **eBay Developer Account** - developer.ebay.com, create app, get API keys
2. **Discord Webhook** - channel → Integrations → Webhooks → copy URL
3. **HiBid credentials** - existing login

## Key Design Decisions
- Minimum increment bidding (never reveal max) with soft close re-engagement
- eBay API for reliable automated pricing (free tier)
- Amazon as manual link only (their API requires affiliate sales, scraping breaks constantly)
- Persistent Playwright browser profile to avoid bot detection on HiBid
- SQLite over external DB (simple, no extra services)
- Ontario HST hardcoded at 13%
- Buyer's premium configurable per auction house (14-16% range typical)
