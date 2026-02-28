# HiBid Sniper - Deployment & Operations

## Quick Start

```bash
cd /home/htpc/hibid-sniper

# Configure credentials
cp .env.example .env
# Edit .env with your HiBid credentials and Discord webhook

# Build and start
docker compose up -d --build

# Access UI
open http://localhost:8199
```

## Docker Container

### Build
```bash
docker compose build
# Uses python:3.12-slim base
# Installs Chromium + Playwright system deps
# ~800MB image size
```

### Start / Stop / Restart
```bash
docker compose up -d          # Start (detached)
docker compose down            # Stop and remove container
docker compose restart         # Restart without rebuilding
docker compose up -d --build   # Rebuild and start
```

### Logs
```bash
docker logs hibid-sniper              # All logs
docker logs hibid-sniper --tail 50    # Last 50 lines
docker logs hibid-sniper -f           # Follow live
```

### Shell Access
```bash
docker exec -it hibid-sniper bash
# Inside container:
#   python -c "from backend.db import get_engine; print(get_engine().url)"
#   ls /app/data/
#   ls /app/browser_profile/
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HIBID_EMAIL` | Yes | - | HiBid login email |
| `HIBID_PASSWORD` | Yes | - | HiBid login password |
| `DISCORD_WEBHOOK_URL` | No | empty | Discord webhook for notifications |
| `HIBID_DB_PATH` | No | `hibid_sniper.db` | SQLite database file path |
| `EMERGENCY_BID_HARD_MAX` | No | `0` | Absolute per-bid hard stop. `0` disables this guard. |

## Persistent Data

### Database
- **Host:** `./data/hibid_sniper.db`
- **Container:** `/app/data/hibid_sniper.db`
- **Contains:** Auction houses, snipe records, deal check history
- **Backup:** `cp ./data/hibid_sniper.db ./data/hibid_sniper.db.bak`

### Browser Profile
- **Host:** `./browser_profile/`
- **Container:** `/app/browser_profile/`
- **Contains:** Chromium cookies, HiBid session, local storage
- **Purpose:** Keeps you logged into HiBid between restarts

## Testing

```bash
# Run all tests
cd /home/htpc/hibid-sniper
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_calculator.py -v

# Run with coverage
python -m pytest tests/ --cov=backend --cov-report=term
```

## Troubleshooting

### Container won't start
```bash
docker logs hibid-sniper
# Check for Python import errors or port conflicts
```

### Port 8199 already in use
```bash
sudo lsof -i :8199
# Kill conflicting process or change port in docker-compose.yml
```

### eBay prices not loading
- Startpage may be rate-limiting or down
- Check: `curl -s "https://www.startpage.com/sp/search?query=test" | head -20`
- Fallback: Click the search buttons to check manually

### HiBid scraping fails
- HiBid may have changed their HTML structure
- Check browser_profile/ isn't corrupted: `rm -rf browser_profile/ && docker compose restart`
- Login may have expired: Container will re-authenticate on next snipe

### Database corruption
```bash
# Backup current
cp ./data/hibid_sniper.db ./data/hibid_sniper.db.corrupt

# Reset (loses all data)
rm ./data/hibid_sniper.db
docker compose restart
# Tables auto-created on startup
```

### Snipes lost after restart
Active snipes are in-memory only. After container restart:
1. Check History tab for completed snipes
2. Re-queue any snipes that were "watching" status
3. Consider: snipes in "bidding" status may have placed a bid before shutdown

## File Permissions

```bash
chmod 600 .env                    # Credentials: owner-only
chmod 700 browser_profile/        # Session data: owner-only
chmod 644 data/hibid_sniper.db    # Database: owner write, group/other read
```

## Network

- **Internal:** Container listens on 0.0.0.0:8199
- **External:** Accessible at localhost:8199 from LAN
- **NOT** exposed to internet (no port forwarding configured)
- **Outbound:** startpage.com, hibid.com, discord.com/api/webhooks (all HTTPS)
