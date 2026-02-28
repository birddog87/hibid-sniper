# HiBid Sniper - Security Analysis

## Threat Model

This is a **local-only tool** running on a home server (localhost). It is NOT designed for public internet exposure. The threat model assumes:
- Attacker must be on the local network (192.168.1.x) or have physical access
- The server is behind a home router/NAT (no port forwarding to internet)
- Trusted users only on the local network

---

## Credential Management

### HiBid Account Credentials
| Item | Detail |
|------|--------|
| Storage | `.env` file at `/home/htpc/hibid-sniper/.env` |
| Format | Plaintext: `HIBID_EMAIL=<your_email>`, `HIBID_PASSWORD=<your_password>` |
| Loaded by | `python-dotenv` at startup (`load_dotenv()`) |
| Used in | `backend/hibid_auth.py` reads from `os.environ` |
| In Docker | Passed via `env_file: .env` in docker-compose.yml |
| Git status | `.env` is in `.gitignore` - will NOT be committed |

**Risk:** Plaintext credentials on disk. Anyone with file access can read them.
**Mitigation:** File permissions should be `600` (owner-only). Docker secrets would be more secure but overkill for local use.

### Discord Webhook URL
| Item | Detail |
|------|--------|
| Storage | `.env` file: `DISCORD_WEBHOOK_URL=` (currently empty) |
| Risk | Webhook URLs are bearer tokens - anyone with the URL can post messages |
| Mitigation | Not committed to git. Keep secret if configured. |

### Database
| Item | Detail |
|------|--------|
| Storage | SQLite file at `./data/hibid_sniper.db` |
| Contains | Deal check history, snipe records, auction house configs |
| Sensitive data | Item names, bid prices, lot URLs - moderate sensitivity |
| Risk | No encryption at rest |
| Mitigation | Volume-mounted, file permissions on host |

---

## API Security

### Authentication: NONE
The REST API has **zero authentication**. All endpoints are publicly accessible to anyone who can reach port 8199.

**Why this is acceptable:** Local-only deployment behind NAT. Only household devices can reach it.

**If you ever expose this to the internet, you MUST add:**
1. API key or Bearer token authentication
2. HTTPS/TLS (currently HTTP only)
3. Rate limiting
4. CORS configuration

### Input Validation
| Vector | Protection | Detail |
|--------|-----------|--------|
| SQL Injection | Protected | SQLAlchemy ORM uses parameterized queries exclusively. No raw SQL. |
| XSS (Stored) | Protected | Frontend `esc()` function escapes all user-provided text before rendering in HTML |
| XSS (Reflected) | Partial | API returns JSON (not HTML), but item names stored in DB are displayed in UI via `esc()` |
| Command Injection | N/A | No shell commands executed from user input |
| Path Traversal | N/A | No file operations based on user input |
| SSRF | Low risk | Startpage queries use user-provided item names but only as search terms, not URLs |

### Pydantic Validation
Request bodies are validated by Pydantic models:
- `CalcRequest`: `bid_price` (float), `premium_pct` (float)
- `AuctionHouseCreate`: `name` (str), `premium_pct` (float)
- `SnipeCreate`: `lot_url` (str), `max_cap` (float), `auction_house_id` (int)

Query parameters (deal-check) rely on FastAPI's built-in type coercion.

### CORS
Restricted to specific origins: `hibid.com`, `www.hibid.com`, and the local frontend (`localhost:8199`, `localhost:8199`). Cross-origin requests from other browser origins are blocked. Note: CORS is browser-enforced only; scripts/tools (curl, httpx) bypass it.

---

## Browser Automation Security

### Playwright / Chromium
| Concern | Detail |
|---------|--------|
| Headless mode | Chromium runs headless inside Docker container |
| Anti-detection | `--disable-blink-features=AutomationControlled` flag set |
| Persistent context | Saved at `./browser_profile/` - contains HiBid cookies and session |
| Cookie exposure | Anyone with access to browser_profile can impersonate the HiBid session |
| Network scope | Playwright only connects to HiBid.com and Startpage.com |

### Credential Entry
HiBid login is automated via Playwright:
1. Credentials read from environment variables (not hardcoded)
2. Filled into login form fields programmatically
3. Never logged to stdout or stored in database
4. Persistent browser context reduces re-login frequency

### Anti-Bot Measures
- HiBid may detect automated browsing patterns
- Playwright persistent context helps maintain realistic session behavior
- `AutomationControlled` feature disabled to avoid detection flags
- Risk: HiBid could ban the account if automation is detected

---

## Network Security

### Exposed Ports
| Port | Service | Protocol | Binding |
|------|---------|----------|---------|
| 8199 | FastAPI (uvicorn) | HTTP | 0.0.0.0 (all interfaces) |

### Outbound Connections
| Destination | Purpose | Protocol |
|-------------|---------|----------|
| startpage.com | eBay price proxy | HTTPS |
| hibid.com | Lot scraping, bidding | HTTPS (via Playwright) |
| discord.com/api/webhooks | Notifications | HTTPS |

### TLS/HTTPS
**Not configured.** All local traffic is unencrypted HTTP.
- API responses (including auction data) transit in plaintext on the LAN
- HiBid credentials are only sent to HiBid.com over HTTPS (via Playwright)
- Startpage requests use HTTPS

---

## Docker Security

### Container Privileges
- Runs as default user (not root, unless Playwright requires it)
- No `--privileged` flag
- No host network mode (uses bridge network)
- No capability additions

### Volume Mounts
| Host Path | Container Path | Purpose | Risk |
|-----------|---------------|---------|------|
| `./data` | `/app/data` | SQLite DB | R/W access to DB |
| `./browser_profile` | `/app/browser_profile` | Playwright session | Contains auth cookies |
| `./.env` | env_file | Credentials | Read at container start |

### Image Base
- `python:3.12-slim` - minimal Debian-based image
- Playwright Chromium installed separately (not `--with-deps` to avoid broken packages)
- System packages: libnss3, libatk1.0-0, libgbm1, etc. (Chromium dependencies only)

---

## Data Sensitivity Classification

| Data | Sensitivity | Storage | Notes |
|------|------------|---------|-------|
| HiBid email/password | HIGH | .env file | Account credentials |
| Discord webhook URL | MEDIUM | .env file | Bearer token for posting |
| Browser cookies/session | MEDIUM | browser_profile/ | HiBid session impersonation |
| Bid prices / max caps | LOW | SQLite DB | Financial strategy info |
| Item names / lot URLs | LOW | SQLite DB | Browsing history |
| eBay price data | LOW | SQLite DB (JSON) | Public market data |

---

## Recommendations

### If Keeping Local-Only (Current)
1. Set `.env` file permissions to `600`: `chmod 600 .env`
2. Set `browser_profile/` permissions to `700`: `chmod 700 browser_profile/`
3. Ensure router does NOT port-forward 8199
4. Back up `.env` separately from code

### If Ever Exposing to Internet
1. **Add authentication** - API key middleware or OAuth
2. **Add HTTPS** - Reverse proxy (nginx/caddy) with Let's Encrypt
3. **Add rate limiting** - Prevent API abuse
4. ~~**Configure CORS**~~ - Done: restricted to HiBid + local frontend
5. **Encrypt database** - SQLCipher instead of plain SQLite
6. **Use Docker secrets** - Instead of .env file
7. **Add logging/monitoring** - Track unauthorized access attempts
8. **Add input length limits** - Prevent oversized payloads
9. **Network isolation** - Restrict container's outbound to known hosts only
