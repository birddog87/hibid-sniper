# HiBid Sniper

A self-hosted auction sniping tool for [HiBid.com](https://hibid.com). Automatically places bids in the final seconds of an auction, analyzes deal quality against eBay market prices, and tracks your spending with a budget system.

**Built for:** Canadian HiBid auctions (prices in CAD, HST tax calculations, eBay.ca comparisons).

## What It Does

- **Snipe auctions** — queues bids and waits until the last 3 seconds to place them, handling soft-close extensions automatically
- **Deal analyzer** — checks eBay sold/active prices so you know if you're overpaying before you bid
- **Budget system** — global spend cap, per-snipe caps, and exposure tracking so you don't blow your budget
- **Driving costs** — auto-calculates gas cost for round trips to each auction house based on your home address
- **Discord notifications** — get pinged when you win, lose, or get capped out
- **Bookmarklet** — one-click from any HiBid lot page to analyze and queue a snipe

## Prerequisites

- **Docker** and **Docker Compose** (that's it)
- A [HiBid.com](https://hibid.com) account

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/hibid-sniper.git
cd hibid-sniper
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` in a text editor and fill in your details:

```env
# Your HiBid login (the email and password you use on hibid.com)
HIBID_EMAIL=your_email@example.com
HIBID_PASSWORD=your_password

# Optional: Discord webhook URL for notifications
DISCORD_WEBHOOK_URL=

# Your server's address — change this to your machine's LAN IP
# if you want to access it from other devices on your network
APP_URL=http://localhost:8199

# Leave this alone unless you know what you're doing
HIBID_DB_PATH=/app/data/hibid_sniper.db
```

**Finding your LAN IP (optional, for access from other devices):**

```bash
# Linux/Mac
hostname -I | awk '{print $1}'

# Windows
ipconfig | findstr IPv4
```

If your IP is `192.168.1.50`, set `APP_URL=http://192.168.1.50:8199`.

### 3. Start the app

```bash
docker compose up -d
```

First run takes 1-2 minutes to build (downloads Python, Playwright, Chromium).

### 4. Open the web UI

Open your browser and go to:

```
http://localhost:8199
```

(Or `http://YOUR_LAN_IP:8199` if you set a different `APP_URL`.)

## Initial Setup (do this once)

### Step 1: Set your budget

Go to the **Setup** tab and configure:

| Setting | What it does | Example |
|---------|-------------|---------|
| Global Spend Cap | Maximum total you can spend across all auctions | `500` |
| Max Single Snipe Cap | Maximum bid for any single item | `200` |

The app will block bids that would exceed these limits.

### Step 2: Set your driving costs (optional)

Still in the **Setup** tab, under "Driving Costs":

| Setting | What it does | Default |
|---------|-------------|---------|
| Home Address | Your address — used to calculate distance to auction houses | (empty) |
| Gas Price ($/L) | Current gas price per liter | `1.80` |
| Fuel Consumption (L/100km) | Your vehicle's fuel consumption | `11.6` |

### Step 3: Add your auction houses

Go to the **Auction Houses** tab and add each auction house you bid at:

| Field | Required? | Notes |
|-------|-----------|-------|
| Name | Yes | e.g. "Walters Auctions" |
| Buyer's Premium % | Yes | The auction house's premium (e.g. `18`) |
| Per-Item Fee ($) | No | Some houses charge a flat fee per item on top of premium |
| Address | No | If you entered your home address, this auto-calculates driving distance and gas cost |
| Distance (km) | No | Auto-filled from address, or enter manually |
| Drive Time (min) | No | Auto-filled from address, or enter manually |

### Step 4: Install the bookmarklet

Go to the **Setup** tab and find the **"Snipe This Lot"** purple button. Drag it to your browser's bookmarks bar.

**If you can't see your bookmarks bar:**
- Chrome: `Ctrl+Shift+B` (or `Cmd+Shift+B` on Mac)
- Firefox: `Ctrl+Shift+B` then right-click the toolbar → "Bookmarks Toolbar"
- Edge: `Ctrl+Shift+B`

## How to Use

### Sniping a lot

1. Browse [HiBid.com](https://hibid.com) and find a lot you want
2. Click the **"Snipe This Lot"** bookmarklet in your bookmarks bar
3. An overlay appears showing:
   - Current bid and increment
   - Select the auction house
   - Click **"Check Market Value"** — it searches eBay for comparable prices
4. Review the deal analysis (good deal / fair / overpriced)
5. Enter your **Max Cap** — the highest you're willing to go
6. Click **"Queue Snipe"**

The sniper will:
- Sleep until 5 minutes before the auction ends (uses no resources while sleeping)
- Wake up, open a browser, log in to HiBid
- Monitor the auction in real-time
- Place a bid in the final 3 seconds
- Handle soft-close extensions (keeps bidding if the timer resets)
- Stop if the price exceeds your cap

### Managing snipes

In the **Snipe Queue** tab:
- Click the **Max Cap** value to edit it
- Use the **Bid** button to manually place a bid at any time
- **Cancel** to remove a snipe

### Checking deal quality without sniping

Use the **Deal Analyzer** tab to check if a price is fair without queuing a snipe. Enter the item name and bid price, and it'll compare against eBay market data.

## Architecture

```
                 Your Browser
                      |
        ┌─────────────┴──────────────┐
        |                            |
   HiBid.com                  Sniper Web UI
   (bookmarklet)              (localhost:8199)
        |                            |
        └─────────────┬──────────────┘
                      |
              ┌───────┴───────┐
              | FastAPI       |
              | Backend       |
              |               |
              | - Snipe jobs  |
              | - Budget mgr  |
              | - eBay search |
              | - Deal calc   |
              └───────┬───────┘
                      |
            ┌─────────┼─────────┐
            |         |         |
         SQLite   Playwright   Discord
         (data)   (Chromium)   (webhooks)
                  (bids on
                   HiBid)
```

## Troubleshooting

### "Cannot reach sniper app" when clicking bookmarklet

The bookmarklet can't connect to your server. Check:
1. Is the container running? `docker compose ps`
2. Is the URL correct? The bookmarklet auto-detects the URL from where it was loaded. If you loaded the app at `http://192.168.1.50:8199`, the bookmarklet will use that URL.
3. Check container logs: `docker compose logs -f`

### Snipe stuck on "SCHEDULED"

This is normal — scheduled snipes sleep until 5 minutes before the auction ends. They'll wake up automatically. Check the "Ends In" column for the countdown.

### Bid not placing

1. Check that your HiBid credentials are correct in `.env`
2. The sniper needs to be logged in — it does this automatically, but if your session expired, restart the container: `docker compose restart`
3. Check logs for errors: `docker compose logs -f`

### "Budget blocked" when trying to bid

Your bid would exceed your budget limits. Either:
- Increase your Global Spend Cap in the Setup tab
- Cancel some other snipes to free up budget
- Lower this snipe's max cap

### Distance not calculating for an auction house

1. Make sure your home address is set in the Setup tab
2. The address needs to be geocodable — use a format like "123 Main St, City, ON"
3. Avoid unit/suite numbers in the address (the geocoder strips them, but unusual formats may fail)
4. Check container logs for geocoding errors: `docker compose logs | grep geocod`

### Container won't start

```bash
# Check what's wrong
docker compose logs

# Rebuild from scratch
docker compose down
docker compose up -d --build
```

## Updating

```bash
git pull
docker compose up -d --build
```

Your data is stored in `./data/` which is mounted as a volume — it persists across rebuilds.

## Configuration Reference

### Environment Variables (`.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HIBID_EMAIL` | Yes | — | Your HiBid login email |
| `HIBID_PASSWORD` | Yes | — | Your HiBid password |
| `DISCORD_WEBHOOK_URL` | No | (empty) | Discord webhook for notifications |
| `APP_URL` | No | `http://localhost:8199` | Your server's URL (for CORS) |
| `HIBID_DB_PATH` | No | `/app/data/hibid_sniper.db` | Database path inside container |

### Ports

| Port | Service |
|------|---------|
| `8199` | Web UI and API |

### Data Storage

| Path | Contents |
|------|----------|
| `./data/` | SQLite database (auction houses, snipes, bid history, settings) |
| `./browser_profile/` | Playwright browser profile (login cookies) |

Both are gitignored and persist across container rebuilds.

## License

MIT — see [LICENSE](LICENSE).
