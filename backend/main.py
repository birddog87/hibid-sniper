import asyncio
import json
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_log_fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
_log_handlers = [logging.StreamHandler()]  # stdout (docker logs)
_startup_log_warning = None

try:
    _file_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "sniper.log"),
        when="midnight",
        backupCount=14,  # keep 2 weeks
        utc=True,
    )
    _file_handler.setFormatter(logging.Formatter(_log_fmt))
    _log_handlers.append(_file_handler)  # persistent file
except OSError as exc:
    _startup_log_warning = f"File logging disabled: {exc}"

logging.basicConfig(level=logging.INFO, format=_log_fmt, handlers=_log_handlers)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()

from backend.db import get_engine, init_db
from backend.models import AuctionHouse, Snipe, DealCheck, Settings, BidLog, WatchlistSearch, WatchlistResult
from backend.calculator import calculate_true_cost, get_verdict
from backend.ebay import search_ebay, build_amazon_search_url
from backend.sniper import SnipeJob, TERMINAL_SNIPE_STATUSES
from backend.hibid_api import place_bid_direct
from backend.hibid_scraper import parse_lot_id_from_url
from backend.watchlist import run_watchlist_scan

logger = logging.getLogger(__name__)
if _startup_log_warning:
    logger.warning(_startup_log_warning)

active_snipes: dict[int, SnipeJob] = {}
COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "hibid_cookies.json")


def init_app_db():
    engine = get_engine()
    init_db(engine)


_watchlist_task = None
_keepalive_task = None

KEEPALIVE_INTERVAL = 1200  # 20 minutes


async def _session_keepalive():
    """Ping HiBid every 20 minutes to keep session alive and capture refreshed JWT."""
    from backend.hibid_scraper import get_browser, _browser_alive
    while True:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not active_snipes:
                continue  # No active snipes, no need to keep session alive
            # Start browser if needed — keepalive must run even while snipes sleep
            browser = await get_browser()
            page = None
            try:
                page = await browser.new_page()
                await page.goto("https://www.hibid.com", wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                # Check if we're still logged in
                logged_in = await page.evaluate("""() => {
                    return !document.querySelector('.login-link');
                }""")
                if logged_in:
                    # Capture any refreshed cookies from HiBid and save to file
                    # HiBid may issue a new JWT on page visit, extending the 7-day expiry
                    try:
                        all_cookies = await browser.cookies(["https://hibid.com", "https://www.hibid.com"])
                        hibid_cookies = [c for c in all_cookies if "hibid" in c.get("domain", "").lower()]
                        if hibid_cookies:
                            # Convert Playwright format back to Cookie Editor format for the JSON file
                            saved = []
                            ss_map = {"None": "no_restriction", "Lax": "lax", "Strict": "strict"}
                            for c in hibid_cookies:
                                entry = {
                                    "name": c["name"],
                                    "value": c["value"],
                                    "domain": c.get("domain", ""),
                                    "path": c.get("path", "/"),
                                }
                                if c.get("expires", -1) > 0:
                                    entry["expirationDate"] = c["expires"]
                                if c.get("httpOnly"):
                                    entry["httpOnly"] = True
                                if c.get("secure"):
                                    entry["secure"] = True
                                ss = c.get("sameSite", "")
                                if ss in ss_map:
                                    entry["sameSite"] = ss_map[ss]
                                saved.append(entry)
                            os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
                            with open(COOKIE_FILE, "w") as f:
                                json.dump(saved, f)
                            # Check if token was refreshed
                            for c in hibid_cookies:
                                if c["name"] == "sessionId":
                                    import base64
                                    parts = c["value"].split(".")
                                    if len(parts) >= 2:
                                        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                                        data = json.loads(base64.b64decode(payload))
                                        exp = data.get("exp", 0)
                                        from datetime import datetime as _dt
                                        exp_str = _dt.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                                        logger.info(f"Session keepalive: alive, JWT expires {exp_str}, {len(saved)} cookies saved")
                                    break
                            else:
                                logger.info(f"Session keepalive: alive, {len(saved)} cookies saved")
                    except Exception as e:
                        logger.warning(f"Session keepalive: alive but cookie save failed: {e}")
                else:
                    logger.warning("Session keepalive: NOT logged in — cookies expired, import fresh ones")
            finally:
                if page:
                    await page.close()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Session keepalive error: {e}")


async def _watchlist_scheduler():
    """Run watchlist scans at 7 AM and 7 PM Eastern."""
    from datetime import timedelta as _td
    ET = timezone(_td(hours=-4))  # EDT; close enough year-round for scheduling
    while True:
        try:
            now = datetime.now(ET)
            # Find next 7:00 or 19:00
            targets = [now.replace(hour=7, minute=0, second=0, microsecond=0),
                       now.replace(hour=19, minute=0, second=0, microsecond=0)]
            future = [t for t in targets if t > now]
            if not future:
                # Both passed today, next is 7 AM tomorrow
                future = [targets[0] + _td(days=1)]
            next_run = min(future)
            wait_secs = (next_run - now).total_seconds()
            logger.info(f"Watchlist scheduler: next scan at {next_run.strftime('%Y-%m-%d %H:%M')} ET ({wait_secs:.0f}s)")
            await asyncio.sleep(wait_secs)
            logger.info("Watchlist scheduler: starting scan")
            summary = await run_watchlist_scan()
            logger.info(f"Watchlist scheduler: scan complete — {summary}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Watchlist scheduler error: {e}")
            await asyncio.sleep(300)  # Retry in 5 min on error


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _watchlist_task
    init_app_db()
    # Resume snipes that were active before container restart
    await _resume_active_snipes()
    _watchlist_task = asyncio.create_task(_watchlist_scheduler())
    _keepalive_task = asyncio.create_task(_session_keepalive())
    yield
    _watchlist_task.cancel()
    if _keepalive_task:
        _keepalive_task.cancel()
    for job in active_snipes.values():
        job.cancel()


async def _resume_active_snipes():
    """Recreate SnipeJobs for any snipes that were running before a restart."""
    engine = get_engine()
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        stale_bidding = session.query(Snipe).filter(
            Snipe.status == "bidding",
            Snipe.end_time.isnot(None),
        ).all()
        stale_count = 0
        for snipe in stale_bidding:
            end = snipe.end_time
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if end < now:
                snipe.status = "error"
                stale_count += 1
        if stale_count:
            session.commit()
            logger.warning(f"Marked {stale_count} stale bidding snipe(s) as error during startup recovery")

        pending = session.query(Snipe).filter(
            Snipe.status.in_(["scheduled", "watching", "bidding"])
        ).all()
        if not pending:
            return

        logger.info(f"Resuming {len(pending)} active snipe(s) from DB")
        for snipe in pending:
            house = session.get(AuctionHouse, snipe.auction_house_id)
            if not house:
                logger.warning(f"Snipe {snipe.id}: auction house {snipe.auction_house_id} not found, skipping")
                continue

            def _get_budget():
                with Session(get_engine()) as sess:
                    return get_budget_status(sess)

            def _make_log_bid(sid, title, url):
                def _log_bid(snipe_id, bid_amount, result, message=""):
                    with Session(get_engine()) as sess:
                        log_bid_attempt(sess, snipe_id, title or "Unknown",
                                       url, bid_amount, result, message)
                return _log_bid

            job = SnipeJob(
                lot_url=snipe.lot_url,
                max_cap=snipe.max_cap,
                premium_pct=house.premium_pct,
                snipe_id=snipe.id,
                end_time=snipe.end_time,
                get_budget=_get_budget,
                log_bid=_make_log_bid(snipe.id, snipe.lot_title, snipe.lot_url),
                db_our_last_bid=snipe.our_last_bid,
                end_time_estimated=_end_time_is_estimated(snipe),
            )
            active_snipes[snipe.id] = job

            sid = snipe.id  # capture for closure

            async def make_update_status(snipe_id):
                async def update_status(j: SnipeJob):
                    with Session(get_engine()) as s:
                        db_snipe = s.get(Snipe, snipe_id)
                        if db_snipe:
                            db_snipe.status = j.status
                            db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                            if j.end_time and j.end_time != db_snipe.end_time:
                                db_snipe.end_time = j.end_time
                            if j.last_bid_placed is not None:
                                db_snipe.our_last_bid = j.last_bid_placed
                            if j.status == "won":
                                db_snipe.winning_bid = getattr(j, 'last_known_price', None) or j.last_bid_placed or db_snipe.current_bid
                            s.commit()
                    if j.status in TERMINAL_SNIPE_STATUSES:
                        active_snipes.pop(snipe_id, None)
                return update_status

            cb = await make_update_status(sid)
            asyncio.create_task(job.run(on_status_change=cb))
            logger.info(f"Resumed snipe {snipe.id}: {snipe.lot_title} (status={snipe.status})")


app = FastAPI(title="HiBid Sniper", lifespan=lifespan)

# CORS: allow HiBid + the app's own origin (configurable via APP_URL env var)
_app_url = os.environ.get("APP_URL", "http://localhost:8199").rstrip("/")
_cors_origins = [
    "https://hibid.com",
    "https://www.hibid.com",
    _app_url,
    "http://localhost:8199",
]
# Deduplicate while preserving order
_cors_origins = list(dict.fromkeys(_cors_origins))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


class CalcRequest(BaseModel):
    bid_price: float
    premium_pct: float
    per_item_fee: float = 0.0


class AuctionHouseCreate(BaseModel):
    name: str
    premium_pct: float
    per_item_fee: float = 0.0
    address: str | None = None
    distance_km: float | None = None
    drive_minutes: float | None = None
    auctioneer_id: int | None = None
    always_include: bool = False


class SnipeCreate(BaseModel):
    lot_url: str
    max_cap: float
    auction_house_id: int


class BrowserSnipeCreate(BaseModel):
    lot_url: str
    lot_title: str | None = None
    lot_id: str | None = None
    current_bid: float | None = None
    increment: float | None = None
    thumbnail_url: str | None = None
    max_cap: float
    auction_house_id: int
    end_time: str | None = None


class CookieImport(BaseModel):
    cookies: list[dict]


class ManualBidRequest(BaseModel):
    bid_amount: float


class SettingsUpdate(BaseModel):
    global_spend_cap: float
    max_single_snipe_cap: float
    home_address: str | None = None
    gas_price_per_liter: float | None = None
    fuel_consumption: float | None = None
    watchlist_postal_code: str | None = None
    watchlist_radius_km: int | None = None


class WatchlistSearchCreate(BaseModel):
    search_term: str


class WatchlistSearchUpdate(BaseModel):
    enabled: bool | None = None
    search_term: str | None = None


class WatchlistSnipeRequest(BaseModel):
    max_cap: float
    auction_house_id: int


logger = logging.getLogger(__name__)


def _end_time_is_estimated(snipe: Snipe) -> bool:
    """Queued times are only estimates until refreshed from a live page/session."""
    if not snipe.end_time:
        return False

    active_job = active_snipes.get(snipe.id)
    if active_job and getattr(active_job, "end_time_reliable", False):
        return False

    if snipe.updated_at and snipe.created_at:
        # A later update typically means we refreshed from the live page or the active
        # job already corrected the stored end_time.
        if (snipe.updated_at - snipe.created_at).total_seconds() > 5:
            return False

    return True


def _parse_end_time_from_text(text: str) -> datetime | None:
    """Parse end time from scraper output.

    Handles three formats:
      CLOSES_AT:M/D/YYYY H:MM:SS AM/PM TZ  — exact close time from Apollo state
      SECS:1234.5                           — seconds remaining from Apollo state
      2d 5h 30m 10s                         — countdown from DOM text (legacy)
    """
    import re
    from datetime import timedelta

    # Format 1: exact close time from Apollo SSR state
    if text.startswith("CLOSES_AT:"):
        close_str = text[len("CLOSES_AT:"):].strip()
        tz_offsets = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}
        m = re.match(
            r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)\s*(\w+)?$",
            close_str, re.IGNORECASE,
        )
        if m:
            hrs = int(m.group(4))
            mins = int(m.group(5))
            secs = int(m.group(6) or 0)
            ampm = m.group(7).upper()
            tz = (m.group(8) or "").upper()
            if ampm == "PM" and hrs != 12:
                hrs += 12
            if ampm == "AM" and hrs == 12:
                hrs = 0
            offset = tz_offsets.get(tz)
            if offset is not None:
                from datetime import timezone as tz_mod
                dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)),
                              hrs, mins, secs, tzinfo=tz_mod(timedelta(hours=offset)))
                return dt.astimezone(timezone.utc)
            else:
                return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)),
                                hrs, mins, secs, tzinfo=timezone.utc)
        return None

    # Format 2: seconds remaining from Apollo SSR state
    if text.startswith("SECS:"):
        try:
            secs_left = float(text[len("SECS:"):])
            if secs_left > 0:
                return datetime.now(timezone.utc) + timedelta(seconds=secs_left)
        except ValueError:
            pass
        return None

    # Format 3: legacy countdown from DOM text (e.g. "Time Remaining: 2d 5h 30m 10s - ...")
    total = 0
    days = re.search(r"(\d+)\s*d", text)
    hours = re.search(r"(\d+)\s*h", text)
    minutes = re.search(r"(\d+)\s*m(?!a)", text)
    seconds = re.search(r"(\d+)\s*s", text)
    if days:
        total += int(days.group(1)) * 86400
    if hours:
        total += int(hours.group(1)) * 3600
    if minutes:
        total += int(minutes.group(1)) * 60
    if seconds:
        total += int(seconds.group(1))
    if total > 0:
        return datetime.now(timezone.utc) + timedelta(seconds=total)
    return None


def get_budget_status(session: Session) -> dict:
    hard_max = 0.0
    raw_hard_max = os.environ.get("EMERGENCY_BID_HARD_MAX", "0").strip()
    try:
        hard_max = float(raw_hard_max)
    except ValueError:
        logger.warning(f"Invalid EMERGENCY_BID_HARD_MAX='{raw_hard_max}', ignoring")
        hard_max = 0.0
    if hard_max < 0:
        hard_max = 0.0

    settings = session.get(Settings, 1)
    cap = settings.global_spend_cap if settings else 0.0
    max_single = settings.max_single_snipe_cap if settings else 200.0

    # Only count money spent TODAY (won auctions), resets each day
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    won = session.query(Snipe).filter(
        Snipe.status == "won",
        Snipe.updated_at >= today_start,
    ).all()
    spent = sum((s.winning_bid or s.current_bid or 0.0) for s in won)

    # "If you win" exposure: current bid on all active snipes
    active = session.query(Snipe).filter(Snipe.status.in_(["scheduled", "watching", "bidding"])).all()
    exposure = sum((s.our_last_bid or s.current_bid or 0.0) for s in active)

    remaining = max(0.0, cap - spent)
    pct_used = (spent / cap * 100) if cap > 0 else 100.0
    exposure_total = spent + exposure
    exposure_pct = (exposure_total / cap * 100) if cap > 0 else 100.0

    return {
        "global_spend_cap": cap,
        "max_single_snipe_cap": max_single,
        "spent": round(spent, 2),
        "remaining": round(remaining, 2),
        "pct_used": round(min(pct_used, 100.0), 1),
        "exposure": round(exposure, 2),
        "exposure_total": round(exposure_total, 2),
        "exposure_pct": round(min(exposure_pct, 100.0), 1),
        "emergency_bid_hard_max": round(hard_max, 2),
    }


def _validate_budget(budget: dict, requested_cap: float):
    if budget["global_spend_cap"] == 0:
        raise HTTPException(400, "Global spend cap not set. Configure your budget in the Setup tab first.")
    if requested_cap > budget["max_single_snipe_cap"]:
        raise HTTPException(400,
            f"Cap ${requested_cap:.2f} exceeds single-snipe limit of ${budget['max_single_snipe_cap']:.2f}.")
    if requested_cap > budget["remaining"]:
        raise HTTPException(400,
            f"Exceeds remaining budget. Spent: ${budget['spent']:.2f}/${budget['global_spend_cap']:.2f}. "
            f"Remaining: ${budget['remaining']:.2f}.")


def _projected_exposure_total(budget: dict, previous_commitment: float, new_amount: float) -> float:
    prev = max(previous_commitment or 0.0, 0.0)
    return float(budget.get("exposure_total", 0.0)) - prev + float(new_amount)


def _validate_bid_safety(
    budget: dict,
    bid_amount: float,
    snipe_cap: float,
    previous_commitment: float,
):
    hard_max = float(budget.get("emergency_bid_hard_max", 0.0) or 0.0)
    if hard_max > 0 and bid_amount > hard_max:
        raise HTTPException(400, f"Bid ${bid_amount:.2f} exceeds emergency hard max of ${hard_max:.2f}")
    if budget["global_spend_cap"] == 0:
        raise HTTPException(400, "Global spend cap not set. Configure your budget in the Setup tab first.")
    if bid_amount > snipe_cap:
        raise HTTPException(400, f"Bid ${bid_amount:.2f} exceeds this snipe cap of ${snipe_cap:.2f}")
    if bid_amount > budget["max_single_snipe_cap"]:
        raise HTTPException(
            400,
            f"Bid ${bid_amount:.2f} exceeds single-snipe limit of ${budget['max_single_snipe_cap']:.2f}",
        )
    if bid_amount > budget["remaining"]:
        raise HTTPException(
            400,
            f"Bid ${bid_amount:.2f} exceeds remaining budget ${budget['remaining']:.2f}",
        )

    projected = _projected_exposure_total(budget, previous_commitment, bid_amount)
    if projected > budget["global_spend_cap"]:
        raise HTTPException(
            400,
            f"Bid blocked by exposure safety limit. Projected exposure ${projected:.2f} "
            f"would exceed cap ${budget['global_spend_cap']:.2f}",
        )


def log_bid_attempt(session: Session, snipe_id: int, lot_title: str, lot_url: str,
                    bid_amount: float, result: str, message: str = ""):
    entry = BidLog(
        snipe_id=snipe_id, lot_title=lot_title, lot_url=lot_url,
        bid_amount=bid_amount, result=result, message=message,
    )
    session.add(entry)
    session.commit()


@app.get("/api/health")
@app.get("/health")
def health():
    from backend.hibid_scraper import _browser
    browsers_open = 0
    if _browser is not None:
        try:
            browsers_open = len(_browser.pages)
        except Exception:
            browsers_open = -1

    active = {sid: j.status for sid, j in active_snipes.items()}
    errors = sum(1 for j in active_snipes.values() if j.status in ("error", "auth_failed"))

    return {
        "status": "ok",
        "app": "HiBid Sniper",
        "browsers_open": browsers_open,
        "active_snipes": len(active_snipes),
        "active_jobs": active,
        "errors_recent": errors,
    }


@app.post("/api/calculate")
def calculate(req: CalcRequest):
    return calculate_true_cost(req.bid_price, req.premium_pct, req.per_item_fee)


@app.get("/api/search-ebay")
async def ebay_search(query: str):
    return await search_ebay(query)


@app.post("/api/deal-check")
async def deal_check(item_name: str, bid_price: float, auction_house_id: int):
    engine = get_engine()
    with Session(engine) as session:
        house = session.get(AuctionHouse, auction_house_id)
        if not house:
            raise HTTPException(404, "Auction house not found")
        cost = calculate_true_cost(bid_price, house.premium_pct, house.per_item_fee or 0.0)

        # Scrape eBay prices via Startpage proxy
        ebay = await search_ebay(item_name)

        ebay_avg = ebay["sold"]["avg"] or ebay["active"]["avg"]
        verdict = get_verdict(cost["total"], ebay_avg)
        deal = DealCheck(
            item_name=item_name,
            bid_price=bid_price,
            true_cost=cost["total"],
            ebay_avg_sold=ebay["sold"]["avg"] or ebay["active"]["avg"],
            ebay_low=ebay["sold"]["low"] or ebay["active"]["low"],
            ebay_high=ebay["sold"]["high"] or ebay["active"]["high"],
            ebay_results=json.dumps(ebay),
            amazon_search_url=ebay["amazon_url"],
            verdict=verdict,
            auction_house_id=auction_house_id,
        )
        session.add(deal)
        session.commit()
        session.refresh(deal)
        return {"deal_id": deal.id, "cost": cost, "ebay": ebay, "verdict": verdict}


@app.get("/api/auction-houses")
def list_houses():
    engine = get_engine()
    with Session(engine) as session:
        houses = session.query(AuctionHouse).all()
        # Read fuel settings for gas cost calc
        settings = session.get(Settings, 1)
        gas_price = settings.gas_price_per_liter if settings and settings.gas_price_per_liter else 1.80
        fuel = settings.fuel_consumption if settings and settings.fuel_consumption else 11.6

        result = []
        for h in houses:
            gas_cost = None
            if h.distance_km:
                gas_cost = round(h.distance_km * 2 / 100 * fuel * gas_price, 2)
            result.append({
                "id": h.id, "name": h.name, "premium_pct": h.premium_pct,
                "per_item_fee": h.per_item_fee or 0.0,
                "address": h.address, "distance_km": h.distance_km,
                "drive_minutes": h.drive_minutes, "round_trip_gas_cost": gas_cost,
                "auctioneer_id": h.auctioneer_id,
                "always_include": bool(h.always_include),
            })
        return result


async def _auto_calc_distance(address: str, session) -> dict:
    """If house address and home address are set, auto-calculate distance/time."""
    from backend.distance import get_driving_distance
    settings = session.get(Settings, 1)
    home = settings.home_address if settings else None
    if not home or not address:
        return {}
    result = await get_driving_distance(home, address)
    if result:
        logger.info(f"Auto-calculated: {address} → {result['distance_km']} km, {result['drive_minutes']} min")
        return result
    return {}


@app.post("/api/auction-houses")
async def create_house(req: AuctionHouseCreate):
    engine = get_engine()
    # Auto-calculate distance if address provided but no distance
    distance = req.distance_km
    drive_time = req.drive_minutes
    if req.address and not distance:
        with Session(engine) as session:
            calc = await _auto_calc_distance(req.address, session)
        if calc:
            distance = calc["distance_km"]
            drive_time = calc["drive_minutes"]

    with Session(engine) as session:
        house = AuctionHouse(
            name=req.name, premium_pct=req.premium_pct,
            per_item_fee=req.per_item_fee,
            address=req.address, distance_km=distance,
            drive_minutes=drive_time,
            auctioneer_id=req.auctioneer_id,
            always_include=1 if req.always_include else 0,
        )
        session.add(house)
        session.commit()
        session.refresh(house)
        return {"id": house.id, "name": house.name, "premium_pct": house.premium_pct,
                "per_item_fee": house.per_item_fee or 0.0,
                "address": house.address, "distance_km": house.distance_km,
                "drive_minutes": house.drive_minutes,
                "auctioneer_id": house.auctioneer_id,
                "always_include": bool(house.always_include)}


@app.put("/api/auction-houses/{house_id}")
async def update_house(house_id: int, req: AuctionHouseCreate):
    engine = get_engine()
    # Auto-calculate distance if address provided but no distance
    distance = req.distance_km
    drive_time = req.drive_minutes
    if req.address and not distance:
        with Session(engine) as session:
            calc = await _auto_calc_distance(req.address, session)
        if calc:
            distance = calc["distance_km"]
            drive_time = calc["drive_minutes"]

    with Session(engine) as session:
        house = session.get(AuctionHouse, house_id)
        if not house:
            raise HTTPException(404, "Not found")
        house.name = req.name
        house.premium_pct = req.premium_pct
        house.per_item_fee = req.per_item_fee
        house.address = req.address
        house.distance_km = distance
        house.drive_minutes = drive_time
        house.auctioneer_id = req.auctioneer_id
        house.always_include = 1 if req.always_include else 0
        session.commit()
        session.refresh(house)
        return {"id": house.id, "name": house.name, "premium_pct": house.premium_pct,
                "per_item_fee": house.per_item_fee or 0.0,
                "address": house.address, "distance_km": house.distance_km,
                "drive_minutes": house.drive_minutes,
                "auctioneer_id": house.auctioneer_id,
                "always_include": bool(house.always_include)}


@app.delete("/api/auction-houses/{house_id}")
def delete_house(house_id: int):
    engine = get_engine()
    with Session(engine) as session:
        house = session.get(AuctionHouse, house_id)
        if not house:
            raise HTTPException(404, "Not found")
        session.delete(house)
        session.commit()
        return {"ok": True}


# --- Settings & Budget ---

@app.get("/api/settings")
def get_settings():
    engine = get_engine()
    with Session(engine) as session:
        s = session.get(Settings, 1)
        return {
            "global_spend_cap": s.global_spend_cap if s else 0.0,
            "max_single_snipe_cap": s.max_single_snipe_cap if s else 200.0,
            "home_address": s.home_address if s else None,
            "gas_price_per_liter": s.gas_price_per_liter if s else 1.80,
            "fuel_consumption": s.fuel_consumption if s else 11.6,
            "watchlist_postal_code": s.watchlist_postal_code if s else None,
            "watchlist_radius_km": s.watchlist_radius_km if s else 50,
        }


@app.put("/api/settings")
def update_settings(req: SettingsUpdate):
    if req.global_spend_cap < 0:
        raise HTTPException(400, "global_spend_cap must be >= 0")
    if req.max_single_snipe_cap < 0:
        raise HTTPException(400, "max_single_snipe_cap must be >= 0")
    if req.global_spend_cap > 0 and req.max_single_snipe_cap > req.global_spend_cap:
        raise HTTPException(400, "max_single_snipe_cap cannot exceed global_spend_cap")
    engine = get_engine()
    with Session(engine) as session:
        s = session.get(Settings, 1)
        if not s:
            s = Settings(id=1)
            session.add(s)
        s.global_spend_cap = req.global_spend_cap
        s.max_single_snipe_cap = req.max_single_snipe_cap
        if req.home_address is not None:
            s.home_address = req.home_address
        if req.gas_price_per_liter is not None:
            s.gas_price_per_liter = req.gas_price_per_liter
        if req.fuel_consumption is not None:
            s.fuel_consumption = req.fuel_consumption
        if req.watchlist_postal_code is not None:
            s.watchlist_postal_code = req.watchlist_postal_code
        if req.watchlist_radius_km is not None:
            s.watchlist_radius_km = req.watchlist_radius_km
        session.commit()
    return {"ok": True}


@app.get("/api/budget")
def budget_status():
    engine = get_engine()
    with Session(engine) as session:
        return get_budget_status(session)


@app.get("/api/bid-log")
def get_bid_log(limit: int = 100):
    engine = get_engine()
    with Session(engine) as session:
        logs = session.query(BidLog).order_by(BidLog.created_at.desc()).limit(limit).all()
        return [
            {
                "id": l.id,
                "snipe_id": l.snipe_id,
                "lot_title": l.lot_title,
                "lot_url": l.lot_url,
                "bid_amount": l.bid_amount,
                "result": l.result,
                "message": l.message,
                "created_at": str(l.created_at),
            }
            for l in logs
        ]


@app.get("/api/snipes")
def list_snipes():
    """Return active/upcoming snipes. Terminal snipes auto-hide after 3 hours."""
    engine = get_engine()
    TERMINAL = {"won", "lost", "capped_out", "error", "auth_failed"}
    GRACE_HOURS = 3
    with Session(engine) as session:
        snipes = session.query(Snipe).filter(Snipe.status != "cancelled").all()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=GRACE_HOURS)
        result = []
        for s in snipes:
            if s.status in TERMINAL:
                finished_at = s.updated_at or s.created_at
                if finished_at:
                    fa = finished_at.replace(tzinfo=timezone.utc) if finished_at.tzinfo is None else finished_at
                    if fa < cutoff:
                        continue  # Hide from queue — visible in History tab
            result.append({
                "id": s.id,
                "lot_url": s.lot_url,
                "lot_title": s.lot_title,
                "max_cap": s.max_cap,
                "current_bid": s.current_bid,
                "status": s.status,
                "thumbnail_url": s.thumbnail_url,
                "end_time": (s.end_time.isoformat() + "Z") if s.end_time else None,
                "end_time_estimated": _end_time_is_estimated(s),
                "our_last_bid": s.our_last_bid,
                "winning_bid": s.winning_bid,
                "increment": s.increment,
                "auction_house_id": s.auction_house_id,
                "updated_at": (s.updated_at.isoformat() + "Z") if s.updated_at else None,
            })
        return result


@app.post("/api/snipes")
async def create_snipe(req: SnipeCreate):
    engine = get_engine()
    with Session(engine) as session:
        house = session.get(AuctionHouse, req.auction_house_id)
        if not house:
            raise HTTPException(404, "Auction house not found")

        budget = get_budget_status(session)
        _validate_budget(budget, req.max_cap)

        from backend.hibid_scraper import scrape_lot

        lot = await scrape_lot(req.lot_url)

        # Parse end_time from scraped time-left text (e.g. "Time Remaining: 2d 5h 30m 10s")
        parsed_end_time = _parse_end_time_from_text(lot.end_time) if lot.end_time else None

        snipe = Snipe(
            lot_url=req.lot_url,
            lot_title=lot.title,
            lot_id=lot.lot_id,
            thumbnail_url=lot.thumbnail_url,
            max_cap=req.max_cap,
            current_bid=lot.current_bid,
            increment=lot.increment,
            end_time=parsed_end_time,
            status="scheduled" if parsed_end_time else "watching",
            auction_house_id=req.auction_house_id,
        )
        session.add(snipe)
        session.commit()
        session.refresh(snipe)

        def _get_budget():
            with Session(get_engine()) as sess:
                return get_budget_status(sess)

        def _log_bid(snipe_id, bid_amount, result, message=""):
            with Session(get_engine()) as sess:
                log_bid_attempt(sess, snipe_id, snipe.lot_title or "Unknown",
                               snipe.lot_url, bid_amount, result, message)

        job = SnipeJob(
            lot_url=req.lot_url,
            max_cap=req.max_cap,
            premium_pct=house.premium_pct,
            snipe_id=snipe.id,
            end_time=parsed_end_time,
            get_budget=_get_budget,
            log_bid=_log_bid,
            end_time_estimated=bool(parsed_end_time),
        )
        active_snipes[snipe.id] = job

        async def update_status(j: SnipeJob):
            with Session(engine) as s:
                db_snipe = s.get(Snipe, snipe.id)
                if db_snipe:
                    db_snipe.status = j.status
                    db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                    if j.end_time and j.end_time != db_snipe.end_time:
                        db_snipe.end_time = j.end_time
                    if j.last_bid_placed is not None:
                        db_snipe.our_last_bid = j.last_bid_placed
                    if j.status in TERMINAL_SNIPE_STATUSES:
                        # Store final sold price — our bid if we placed one, else last known price
                        db_snipe.winning_bid = getattr(j, 'last_known_price', None) or j.last_bid_placed or db_snipe.current_bid
                    s.commit()
            if j.status in TERMINAL_SNIPE_STATUSES:
                active_snipes.pop(snipe.id, None)

        asyncio.create_task(job.run(on_status_change=update_status))
        return {
            "id": snipe.id,
            "lot_title": lot.title,
            "current_bid": lot.current_bid,
            "increment": lot.increment,
            "end_time": (parsed_end_time.isoformat() + "Z") if parsed_end_time else None,
            "status": snipe.status,
        }


@app.post("/api/snipes/cancel-all")
def cancel_all_snipes():
    for job in list(active_snipes.values()):
        job.cancel()
    active_snipes.clear()
    engine = get_engine()
    with Session(engine) as session:
        session.query(Snipe).filter(
            Snipe.status.in_(["scheduled", "watching", "bidding"])
        ).update({"status": "cancelled"}, synchronize_session="fetch")
        session.commit()
    return {"ok": True}


class SnipeUpdate(BaseModel):
    max_cap: float | None = None


@app.put("/api/snipes/{snipe_id}")
def update_snipe(snipe_id: int, req: SnipeUpdate):
    engine = get_engine()
    with Session(engine) as session:
        snipe = session.get(Snipe, snipe_id)
        if not snipe:
            raise HTTPException(404, "Snipe not found")
        if req.max_cap is not None:
            if req.max_cap <= 0:
                raise HTTPException(400, "Max cap must be > 0")
            budget = get_budget_status(session)
            _validate_budget(budget, req.max_cap)
            snipe.max_cap = req.max_cap
            # Also update the in-memory job if running
            if snipe_id in active_snipes:
                active_snipes[snipe_id].max_cap = req.max_cap
        session.commit()
    return {"ok": True}


@app.post("/api/snipes/refresh-times")
async def refresh_snipe_times():
    """Refresh end times for queued snipes using the same live extraction as the sniper."""
    from backend.hibid_scraper import get_browser

    engine = get_engine()
    with Session(engine) as session:
        snipes = session.query(Snipe).filter(Snipe.status.in_(["scheduled", "watching"])).all()
        if not snipes:
            return {"ok": True, "updated": 0}

        browser = await get_browser()
        page = await browser.new_page()
        updated = 0

        try:
            for snipe in snipes:
                try:
                    await page.goto(snipe.lot_url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(3000)
                    temp_job = active_snipes.get(snipe.id) or SnipeJob(
                        lot_url=snipe.lot_url,
                        max_cap=snipe.max_cap,
                        premium_pct=0.0,
                        snipe_id=snipe.id,
                        end_time=snipe.end_time,
                        end_time_estimated=_end_time_is_estimated(snipe),
                    )
                    end = await temp_job._extract_end_time_from_page(page)
                    if not end:
                        continue

                    snipe.end_time = end
                    if snipe.id in active_snipes:
                        active_snipes[snipe.id].end_time = end
                        active_snipes[snipe.id].end_time_reliable = True
                        active_snipes[snipe.id].end_time_estimated = False
                    updated += 1
                    logger.info(f"Snipe {snipe.id}: refreshed end_time to {end.isoformat()}")

                    # Also grab thumbnail if missing
                    if not snipe.thumbnail_url:
                        thumb_url = await page.evaluate("""() => {
                            const el = document.querySelector("[style*='background-image'][style*='cdn.hibid.com']");
                            if (!el) return null;
                            const m = el.style.backgroundImage.match(/url\\("?([^"\\)]+)"?\\)/);
                            return m ? m[1] : null;
                        }""")
                        if thumb_url:
                            snipe.thumbnail_url = thumb_url
                            logger.info(f"Snipe {snipe.id}: updated thumbnail")
                except Exception as e:
                    logger.warning(f"Snipe {snipe.id}: failed to refresh time: {e}")
            session.commit()
        finally:
            await page.close()

    return {"ok": True, "updated": updated}


@app.post("/api/snipes/{snipe_id}/pause")
def pause_snipe(snipe_id: int):
    """Pause a scheduled/watching snipe — stops it from bidding."""
    if snipe_id in active_snipes:
        active_snipes[snipe_id].cancel()
        del active_snipes[snipe_id]
    engine = get_engine()
    with Session(engine) as session:
        snipe = session.get(Snipe, snipe_id)
        if not snipe:
            raise HTTPException(404, "Snipe not found")
        if snipe.status not in ("scheduled", "watching", "bidding"):
            raise HTTPException(400, f"Cannot pause snipe with status '{snipe.status}'")
        snipe.status = "paused"
        session.commit()
    return {"ok": True}


@app.post("/api/snipes/{snipe_id}/resume")
async def resume_snipe(snipe_id: int):
    """Resume a paused snipe — puts it back in the queue."""
    engine = get_engine()
    with Session(engine) as session:
        snipe = session.get(Snipe, snipe_id)
        if not snipe:
            raise HTTPException(404, "Snipe not found")
        if snipe.status not in ("paused", "capped_out", "auth_failed"):
            raise HTTPException(400, f"Cannot resume snipe with status '{snipe.status}'")

        house = session.get(AuctionHouse, snipe.auction_house_id)
        if not house:
            raise HTTPException(404, "Auction house not found")

        snipe.status = "scheduled" if snipe.end_time else "watching"
        session.commit()

        # Re-create the SnipeJob
        def _get_budget():
            with Session(get_engine()) as sess:
                return get_budget_status(sess)

        def _log_bid(sid, bid_amount, result, message=""):
            with Session(get_engine()) as sess:
                log_bid_attempt(sess, snipe_id, snipe.lot_title or "Unknown",
                               snipe.lot_url, bid_amount, result, message)

        job = SnipeJob(
            lot_url=snipe.lot_url,
            max_cap=snipe.max_cap,
            premium_pct=house.premium_pct,
            snipe_id=snipe.id,
            end_time=snipe.end_time,
            get_budget=_get_budget,
            log_bid=_log_bid,
            db_our_last_bid=snipe.our_last_bid,
            end_time_estimated=_end_time_is_estimated(snipe),
        )
        active_snipes[snipe.id] = job

        async def update_status(j: SnipeJob):
            with Session(get_engine()) as s:
                db_snipe = s.get(Snipe, snipe_id)
                if db_snipe:
                    db_snipe.status = j.status
                    db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                    if j.end_time and j.end_time != db_snipe.end_time:
                        db_snipe.end_time = j.end_time
                    if j.last_bid_placed is not None:
                        db_snipe.our_last_bid = j.last_bid_placed
                    if j.status in TERMINAL_SNIPE_STATUSES:
                        # Store final sold price — our bid if we placed one, else last known price
                        db_snipe.winning_bid = getattr(j, 'last_known_price', None) or j.last_bid_placed or db_snipe.current_bid
                    s.commit()
            if j.status in TERMINAL_SNIPE_STATUSES:
                active_snipes.pop(snipe_id, None)

        asyncio.create_task(job.run(on_status_change=update_status))
    return {"ok": True}


@app.post("/api/snipes/pause-all")
def pause_all_snipes():
    """Pause all active snipes."""
    for job in list(active_snipes.values()):
        job.cancel()
    active_snipes.clear()
    engine = get_engine()
    with Session(engine) as session:
        snipes = session.query(Snipe).filter(
            Snipe.status.in_(["scheduled", "watching", "bidding"])
        ).all()
        for snipe in snipes:
            snipe.status = "paused"
        session.commit()
        return {"ok": True, "paused": len(snipes)}


@app.post("/api/snipes/resume-all")
async def resume_all_snipes():
    """Resume all paused snipes."""
    engine = get_engine()
    resumed = 0
    with Session(engine) as session:
        snipes = session.query(Snipe).filter(Snipe.status == "paused").all()
        for snipe in snipes:
            house = session.get(AuctionHouse, snipe.auction_house_id)
            if not house:
                continue
            snipe.status = "scheduled" if snipe.end_time else "watching"

            def _get_budget():
                with Session(get_engine()) as sess:
                    return get_budget_status(sess)

            def _make_log_bid(sid, title, url):
                def _log_bid(snipe_id, bid_amount, result, message=""):
                    with Session(get_engine()) as sess:
                        log_bid_attempt(sess, sid, title or "Unknown",
                                       url, bid_amount, result, message)
                return _log_bid

            job = SnipeJob(
                lot_url=snipe.lot_url,
                max_cap=snipe.max_cap,
                premium_pct=house.premium_pct,
                snipe_id=snipe.id,
                end_time=snipe.end_time,
                get_budget=_get_budget,
                log_bid=_make_log_bid(snipe.id, snipe.lot_title, snipe.lot_url),
                db_our_last_bid=snipe.our_last_bid,
                end_time_estimated=_end_time_is_estimated(snipe),
            )
            active_snipes[snipe.id] = job

            sid = snipe.id
            async def make_update_status(snipe_id):
                async def update_status(j: SnipeJob):
                    with Session(get_engine()) as s:
                        db_snipe = s.get(Snipe, snipe_id)
                        if db_snipe:
                            db_snipe.status = j.status
                            db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                            if j.end_time and j.end_time != db_snipe.end_time:
                                db_snipe.end_time = j.end_time
                            if j.last_bid_placed is not None:
                                db_snipe.our_last_bid = j.last_bid_placed
                            if j.status == "won":
                                db_snipe.winning_bid = getattr(j, 'last_known_price', None) or j.last_bid_placed or db_snipe.current_bid
                            s.commit()
                    if j.status in TERMINAL_SNIPE_STATUSES:
                        active_snipes.pop(snipe_id, None)
                return update_status

            asyncio.create_task(job.run(on_status_change=await make_update_status(sid)))
            resumed += 1
        session.commit()
    return {"ok": True, "resumed": resumed}


@app.post("/api/snipes/{snipe_id}/cancel")
def cancel_snipe(snipe_id: int):
    if snipe_id in active_snipes:
        active_snipes[snipe_id].cancel()
        del active_snipes[snipe_id]
    engine = get_engine()
    with Session(engine) as session:
        snipe = session.get(Snipe, snipe_id)
        if snipe:
            snipe.status = "cancelled"
            session.commit()
    return {"ok": True}


@app.post("/api/snipes/{snipe_id}/bid")
async def manual_bid(snipe_id: int, req: ManualBidRequest):
    """Place a manual bid on a snipe's lot via HiBid GraphQL."""
    if req.bid_amount <= 0:
        raise HTTPException(400, "Bid amount must be > 0")

    engine = get_engine()
    with Session(engine) as session:
        snipe = session.get(Snipe, snipe_id)
        if not snipe:
            raise HTTPException(404, "Snipe not found")
        if snipe.status not in ("scheduled", "watching", "bidding"):
            raise HTTPException(400, f"Cannot bid on snipe with status '{snipe.status}'")

        # Safety checks at bid time (independent of queue-time checks)
        budget = get_budget_status(session)
        previous_commitment = snipe.our_last_bid or snipe.current_bid or 0.0
        _validate_bid_safety(
            budget=budget,
            bid_amount=req.bid_amount,
            snipe_cap=snipe.max_cap,
            previous_commitment=previous_commitment,
        )

        lot_id = int(parse_lot_id_from_url(snipe.lot_url))
        lot_url = snipe.lot_url
        lot_title = snipe.lot_title or "Unknown"

    # Place bid via direct GraphQL (no Playwright)
    result = await place_bid_direct(lot_id, req.bid_amount, lot_url=lot_url)

    # Log and update DB
    with Session(engine) as session:
        log_bid_attempt(session, snipe_id, lot_title,
                       lot_url, req.bid_amount,
                       "placed" if result["success"] else "error",
                       f"{result['status']}: {result['message']}")

        if result["success"]:
            db_snipe = session.get(Snipe, snipe_id)
            if db_snipe:
                db_snipe.our_last_bid = req.bid_amount
                db_snipe.current_bid = req.bid_amount
                session.commit()

    return result


@app.get("/api/history")
def get_history():
    engine = get_engine()
    with Session(engine) as session:
        deals = (
            session.query(DealCheck).order_by(DealCheck.created_at.desc()).limit(50).all()
        )
        snipes = (
            session.query(Snipe)
            .filter(Snipe.status.in_(["won", "lost", "capped_out", "error", "auth_failed"]))
            .order_by(Snipe.updated_at.desc())
            .limit(200)
            .all()
        )
        houses = {h.id: h for h in session.query(AuctionHouse).all()}

        # Gather bid logs for all history snipes in one query
        snipe_ids = [s.id for s in snipes]
        all_logs = (
            session.query(BidLog)
            .filter(BidLog.snipe_id.in_(snipe_ids))
            .order_by(BidLog.created_at.desc())
            .all()
        ) if snipe_ids else []
        logs_by_snipe: dict[int, list] = {}
        for log in all_logs:
            logs_by_snipe.setdefault(log.snipe_id, []).append({
                "bid_amount": log.bid_amount,
                "result": log.result,
                "message": log.message,
                "created_at": (log.created_at.isoformat() + "Z") if log.created_at else None,
            })

        snipe_list = []
        for s in snipes:
            house = houses.get(s.auction_house_id)
            sold_price = s.winning_bid or s.current_bid or 0
            premium_pct = house.premium_pct if house else 0
            per_item_fee = house.per_item_fee if house and house.per_item_fee else 0
            true_cost = sold_price * (1 + (premium_pct or 0) / 100) + per_item_fee if sold_price else None
            snipe_list.append({
                "id": s.id,
                "lot_title": s.lot_title,
                "lot_url": s.lot_url,
                "thumbnail_url": s.thumbnail_url,
                "max_cap": s.max_cap,
                "current_bid": s.current_bid,
                "winning_bid": s.winning_bid,
                "our_last_bid": s.our_last_bid,
                "status": s.status,
                "true_cost": round(true_cost, 2) if true_cost else None,
                "auction_house_name": house.name if house else None,
                "premium_pct": premium_pct,
                "per_item_fee": per_item_fee,
                "end_time": (s.end_time.isoformat() + "Z") if s.end_time else None,
                "created_at": (s.created_at.isoformat() + "Z") if s.created_at else None,
                "updated_at": (s.updated_at.isoformat() + "Z") if s.updated_at else None,
                "bids": logs_by_snipe.get(s.id, []),
            })

        return {
            "deals": [
                {
                    "id": d.id,
                    "item_name": d.item_name,
                    "bid_price": d.bid_price,
                    "true_cost": d.true_cost,
                    "ebay_avg_sold": d.ebay_avg_sold,
                    "verdict": d.verdict,
                    "created_at": str(d.created_at),
                }
                for d in deals
            ],
            "snipes": snipe_list,
        }


async def _pre_register_for_snipe(snipe_id: int, lot_url: str):
    """Best-effort pre-registration at queue time. Non-fatal — sniper retries on wake."""
    try:
        from backend.hibid_api import get_auth_token, _get_auction_id, _register_for_auction
        token = get_auth_token()
        if not token:
            logger.info(f"Snipe {snipe_id}: pre-registration skipped (no auth token)")
            return
        auction_id = await _get_auction_id(lot_url)
        if not auction_id:
            logger.warning(f"Snipe {snipe_id}: pre-registration skipped (could not get auction ID)")
            return
        result = await _register_for_auction(token, auction_id)
        logger.info(f"Snipe {snipe_id}: pre-registered for auction {auction_id}: {result}")
    except Exception as e:
        logger.warning(f"Snipe {snipe_id}: pre-registration failed (non-fatal): {e}")


@app.post("/api/snipes/from-browser")
async def create_snipe_from_browser(req: BrowserSnipeCreate):
    """Create a snipe using data sent directly from the bookmarklet.
    No scraping needed - the browser already has the lot data."""
    engine = get_engine()
    with Session(engine) as session:
        house = session.get(AuctionHouse, req.auction_house_id)
        if not house:
            raise HTTPException(404, "Auction house not found")

        budget = get_budget_status(session)
        _validate_budget(budget, req.max_cap)

        # Parse end_time from ISO string
        parsed_end_time = None
        if req.end_time:
            try:
                parsed_end_time = datetime.fromisoformat(req.end_time.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse end_time: {req.end_time}")

        snipe = Snipe(
            lot_url=req.lot_url,
            lot_title=req.lot_title,
            lot_id=req.lot_id,
            thumbnail_url=req.thumbnail_url,
            max_cap=req.max_cap,
            current_bid=req.current_bid,
            increment=req.increment,
            end_time=parsed_end_time,
            status="scheduled" if parsed_end_time else "watching",
            auction_house_id=req.auction_house_id,
        )
        session.add(snipe)
        session.commit()
        session.refresh(snipe)

        def _get_budget():
            with Session(get_engine()) as sess:
                return get_budget_status(sess)

        def _log_bid(snipe_id, bid_amount, result, message=""):
            with Session(get_engine()) as sess:
                log_bid_attempt(sess, snipe_id, snipe.lot_title or "Unknown",
                               snipe.lot_url, bid_amount, result, message)

        job = SnipeJob(
            lot_url=req.lot_url,
            max_cap=req.max_cap,
            premium_pct=house.premium_pct,
            snipe_id=snipe.id,
            end_time=parsed_end_time,
            get_budget=_get_budget,
            log_bid=_log_bid,
            end_time_estimated=bool(parsed_end_time),
        )
        active_snipes[snipe.id] = job

        async def update_status(j: SnipeJob):
            with Session(engine) as s:
                db_snipe = s.get(Snipe, snipe.id)
                if db_snipe:
                    db_snipe.status = j.status
                    db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                    if j.end_time and j.end_time != db_snipe.end_time:
                        db_snipe.end_time = j.end_time
                    if j.last_bid_placed is not None:
                        db_snipe.our_last_bid = j.last_bid_placed
                    if j.status in TERMINAL_SNIPE_STATUSES:
                        # Store final sold price — our bid if we placed one, else last known price
                        db_snipe.winning_bid = getattr(j, 'last_known_price', None) or j.last_bid_placed or db_snipe.current_bid
                    s.commit()
            if j.status in TERMINAL_SNIPE_STATUSES:
                active_snipes.pop(snipe.id, None)

        asyncio.create_task(job.run(on_status_change=update_status))
        # Pre-register for the auction immediately (best-effort, non-blocking)
        asyncio.create_task(_pre_register_for_snipe(snipe.id, req.lot_url))
        return {
            "id": snipe.id,
            "lot_title": req.lot_title,
            "current_bid": req.current_bid,
            "end_time": (parsed_end_time.isoformat() + "Z") if parsed_end_time else None,
            "status": snipe.status,
        }


@app.post("/api/cookies")
async def import_cookies(req: CookieImport):
    """Import HiBid cookies from the user's browser."""
    # Filter to only HiBid cookies
    hibid_cookies = [c for c in req.cookies if "hibid" in c.get("domain", "").lower()]
    if not hibid_cookies:
        raise HTTPException(400, "No HiBid cookies found in the provided data")

    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    with open(COOKIE_FILE, "w") as f:
        json.dump(hibid_cookies, f)

    # Inject into running browser if it exists
    try:
        from backend.hibid_scraper import inject_cookies
        await inject_cookies(hibid_cookies)
    except Exception:
        pass  # Browser may not be running yet, cookies will load on next start

    return {"ok": True, "count": len(hibid_cookies)}


def _decode_jwt_exp(token_value: str) -> int | None:
    """Extract exp timestamp from a JWT without a library."""
    try:
        import base64
        parts = token_value.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        data = json.loads(base64.b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


@app.get("/api/cookies/status")
def cookie_status():
    """Check if HiBid cookies are loaded, including JWT expiry info."""
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE) as f:
                cookies = json.load(f)
            import time
            session_cookie = next((c for c in cookies if c.get("name") == "sessionId"), None)
            expired = False
            expires_at = None
            jwt_expires_at = None
            jwt_expires_in_hours = None
            if session_cookie:
                if session_cookie.get("expirationDate"):
                    expires_at = session_cookie["expirationDate"]
                    expired = time.time() > expires_at
                # Decode actual JWT expiry (more reliable than cookie metadata)
                jwt_exp = _decode_jwt_exp(session_cookie.get("value", ""))
                if jwt_exp:
                    jwt_expires_at = jwt_exp
                    jwt_expires_in_hours = (jwt_exp - time.time()) / 3600
                    if jwt_expires_in_hours < 0:
                        expired = True
            return {
                "has_cookies": True,
                "count": len(cookies),
                "session_expired": expired,
                "session_expires_at": expires_at,
                "jwt_expires_at": jwt_expires_at,
                "jwt_expires_in_hours": jwt_expires_in_hours,
            }
        except Exception:
            pass
    return {"has_cookies": False, "count": 0, "session_expired": True, "session_expires_at": None, "jwt_expires_at": None, "jwt_expires_in_hours": None}


# ---------------------------------------------------------------------------
# Watchlist endpoints
# ---------------------------------------------------------------------------

@app.get("/api/watchlist/searches")
def list_watchlist_searches():
    engine = get_engine()
    with Session(engine) as session:
        searches = session.query(WatchlistSearch).all()
        return [
            {"id": s.id, "search_term": s.search_term, "enabled": bool(s.enabled),
             "created_at": s.created_at.isoformat() if s.created_at else None}
            for s in searches
        ]


@app.post("/api/watchlist/searches")
def create_watchlist_search(req: WatchlistSearchCreate):
    term = req.search_term.strip()
    if not term:
        raise HTTPException(400, "Search term cannot be empty")
    engine = get_engine()
    with Session(engine) as session:
        existing = session.query(WatchlistSearch).filter(
            WatchlistSearch.search_term == term
        ).first()
        if existing:
            raise HTTPException(400, f"Search '{term}' already exists")
        s = WatchlistSearch(search_term=term, enabled=1)
        session.add(s)
        session.commit()
        session.refresh(s)
        return {"id": s.id, "search_term": s.search_term, "enabled": True}


@app.put("/api/watchlist/searches/{search_id}")
def update_watchlist_search(search_id: int, req: WatchlistSearchUpdate):
    engine = get_engine()
    with Session(engine) as session:
        s = session.get(WatchlistSearch, search_id)
        if not s:
            raise HTTPException(404, "Search not found")
        if req.enabled is not None:
            s.enabled = 1 if req.enabled else 0
        if req.search_term is not None:
            term = req.search_term.strip()
            if not term:
                raise HTTPException(400, "Search term cannot be empty")
            s.search_term = term
        session.commit()
    return {"ok": True}


@app.delete("/api/watchlist/searches/{search_id}")
def delete_watchlist_search(search_id: int):
    engine = get_engine()
    with Session(engine) as session:
        s = session.get(WatchlistSearch, search_id)
        if not s:
            raise HTTPException(404, "Search not found")
        # Delete associated results
        session.query(WatchlistResult).filter(
            WatchlistResult.search_id == search_id
        ).delete()
        session.delete(s)
        session.commit()
    return {"ok": True}


@app.get("/api/watchlist/results")
def list_watchlist_results():
    engine = get_engine()
    with Session(engine) as session:
        now_utc = datetime.utcnow()
        results = session.query(WatchlistResult).filter(
            WatchlistResult.status != "dismissed",
            WatchlistResult.is_closed == 0,
            (WatchlistResult.closes_at > now_utc) | (WatchlistResult.closes_at == None),
        ).order_by(WatchlistResult.closes_at.asc()).all()

        searches = {s.id: s.search_term for s in session.query(WatchlistSearch).all()}
        houses = {h.id: {"name": h.name, "premium_pct": h.premium_pct, "per_item_fee": h.per_item_fee}
                  for h in session.query(AuctionHouse).all()}

        grouped = {}
        for r in results:
            term = searches.get(r.search_id, "Unknown")
            if term not in grouped:
                grouped[term] = []
            house_info = houses.get(r.matched_house_id) if r.matched_house_id else None
            grouped[term].append({
                "id": r.id,
                "hibid_lot_id": r.hibid_lot_id,
                "title": r.title,
                "lot_url": r.lot_url,
                "thumbnail_url": r.thumbnail_url,
                "current_bid": r.current_bid,
                "bid_count": r.bid_count,
                "min_bid": r.min_bid,
                "closes_at": r.closes_at.isoformat() if r.closes_at else None,
                "is_closed": bool(r.is_closed),
                "auction_name": r.auction_name,
                "auction_city": r.auction_city,
                "auctioneer_name": r.auctioneer_name,
                "distance_miles": r.distance_miles,
                "buyer_premium_pct": r.buyer_premium_pct,
                "shipping_offered": bool(r.shipping_offered),
                "currency": r.currency,
                "status": r.status,
                "matched_house_id": r.matched_house_id,
                "matched_house": house_info,
                "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
            })
        return {"groups": grouped, "total": len(results)}


@app.post("/api/watchlist/results/{result_id}/dismiss")
def dismiss_watchlist_result(result_id: int):
    engine = get_engine()
    with Session(engine) as session:
        r = session.get(WatchlistResult, result_id)
        if not r:
            raise HTTPException(404, "Result not found")
        r.status = "dismissed"
        session.commit()
    return {"ok": True}


@app.post("/api/watchlist/results/{result_id}/snipe")
async def snipe_watchlist_result(result_id: int, req: WatchlistSnipeRequest):
    """Create a snipe directly from a watchlist result."""
    engine = get_engine()
    with Session(engine) as session:
        r = session.get(WatchlistResult, result_id)
        if not r:
            raise HTTPException(404, "Result not found")
        house = session.get(AuctionHouse, req.auction_house_id)
        if not house:
            raise HTTPException(404, "Auction house not found")

        budget = get_budget_status(session)
        _validate_budget(budget, req.max_cap)

        # Check not already sniped
        existing = session.query(Snipe).filter(
            Snipe.lot_url == r.lot_url,
            Snipe.status.notin_(["cancelled", "error"]),
        ).first()
        if existing:
            raise HTTPException(400, f"Lot already queued (snipe #{existing.id})")

        snipe = Snipe(
            lot_url=r.lot_url,
            lot_title=r.title,
            lot_id=str(r.hibid_lot_id),
            thumbnail_url=r.thumbnail_url,
            max_cap=req.max_cap,
            current_bid=r.current_bid,
            increment=None,
            end_time=r.closes_at,
            status="scheduled" if r.closes_at else "watching",
            auction_house_id=req.auction_house_id,
        )
        session.add(snipe)
        r.status = "sniped"
        session.commit()
        session.refresh(snipe)

        def _get_budget():
            with Session(get_engine()) as sess:
                return get_budget_status(sess)

        def _log_bid(snipe_id, bid_amount, result, message=""):
            with Session(get_engine()) as sess:
                log_bid_attempt(sess, snipe_id, snipe.lot_title or "Unknown",
                               snipe.lot_url, bid_amount, result, message)

        job = SnipeJob(
            lot_url=snipe.lot_url,
            max_cap=req.max_cap,
            premium_pct=house.premium_pct,
            snipe_id=snipe.id,
            end_time=snipe.end_time,
            get_budget=_get_budget,
            log_bid=_log_bid,
            end_time_estimated=False,
        )
        active_snipes[snipe.id] = job

        async def update_status(j: SnipeJob):
            with Session(engine) as s:
                db_snipe = s.get(Snipe, snipe.id)
                if db_snipe:
                    db_snipe.status = j.status
                    db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                    if j.end_time and j.end_time != db_snipe.end_time:
                        db_snipe.end_time = j.end_time
                    if j.last_bid_placed is not None:
                        db_snipe.our_last_bid = j.last_bid_placed
                    if j.status in TERMINAL_SNIPE_STATUSES:
                        db_snipe.winning_bid = getattr(j, 'last_known_price', None) or j.last_bid_placed or db_snipe.current_bid
                    s.commit()
            if j.status in TERMINAL_SNIPE_STATUSES:
                active_snipes.pop(snipe.id, None)

        asyncio.create_task(job.run(on_status_change=update_status))
        return {
            "id": snipe.id,
            "lot_title": snipe.lot_title,
            "current_bid": snipe.current_bid,
            "end_time": snipe.end_time.isoformat() if snipe.end_time else None,
            "status": snipe.status,
        }


@app.post("/api/watchlist/scan")
async def trigger_watchlist_scan():
    """Manually trigger a watchlist scan."""
    summary = await run_watchlist_scan()
    return summary


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
