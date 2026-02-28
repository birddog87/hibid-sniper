import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()

from backend.db import get_engine, init_db
from backend.models import AuctionHouse, Snipe, DealCheck, Settings, BidLog
from backend.calculator import calculate_true_cost, get_verdict
from backend.ebay import search_ebay, build_amazon_search_url
from backend.sniper import SnipeJob
from backend.hibid_api import place_bid_direct
from backend.hibid_scraper import parse_lot_id_from_url

active_snipes: dict[int, SnipeJob] = {}
COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "hibid_cookies.json")


def init_app_db():
    engine = get_engine()
    init_db(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_app_db()
    # Resume snipes that were active before container restart
    await _resume_active_snipes()
    yield
    for job in active_snipes.values():
        job.cancel()


async def _resume_active_snipes():
    """Recreate SnipeJobs for any snipes that were running before a restart."""
    engine = get_engine()
    with Session(engine) as session:
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
                            if j.last_bid_placed is not None:
                                db_snipe.our_last_bid = j.last_bid_placed
                            if j.status == "won" and j.last_bid_placed:
                                db_snipe.winning_bid = j.last_bid_placed
                            s.commit()
                    if j.status in ("won", "lost", "capped_out"):
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


logger = logging.getLogger(__name__)


def _parse_end_time_from_text(text: str) -> datetime | None:
    """Parse a time-remaining string like '2d 5h 30m 10s' into an absolute datetime."""
    import re
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
        from datetime import timedelta
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

    # Only count actual money spent (won auctions), not theoretical exposure
    won = session.query(Snipe).filter(Snipe.status == "won").all()
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
def health():
    return {"status": "ok"}


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
        )
        session.add(house)
        session.commit()
        session.refresh(house)
        return {"id": house.id, "name": house.name, "premium_pct": house.premium_pct,
                "per_item_fee": house.per_item_fee or 0.0,
                "address": house.address, "distance_km": house.distance_km,
                "drive_minutes": house.drive_minutes}


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
        session.commit()
        session.refresh(house)
        return {"id": house.id, "name": house.name, "premium_pct": house.premium_pct,
                "per_item_fee": house.per_item_fee or 0.0,
                "address": house.address, "distance_km": house.distance_km,
                "drive_minutes": house.drive_minutes}


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
    engine = get_engine()
    with Session(engine) as session:
        snipes = session.query(Snipe).filter(Snipe.status != "cancelled").all()
        return [
            {
                "id": s.id,
                "lot_url": s.lot_url,
                "lot_title": s.lot_title,
                "max_cap": s.max_cap,
                "current_bid": s.current_bid,
                "status": s.status,
                "thumbnail_url": s.thumbnail_url,
                "end_time": (s.end_time.isoformat() + "Z") if s.end_time else None,
                "our_last_bid": s.our_last_bid,
                "increment": s.increment,
                "auction_house_id": s.auction_house_id,
            }
            for s in snipes
        ]


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
        )
        active_snipes[snipe.id] = job

        async def update_status(j: SnipeJob):
            with Session(engine) as s:
                db_snipe = s.get(Snipe, snipe.id)
                if db_snipe:
                    db_snipe.status = j.status
                    db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                    if j.last_bid_placed is not None:
                        db_snipe.our_last_bid = j.last_bid_placed
                    if j.status == "won" and j.last_bid_placed:
                        db_snipe.winning_bid = j.last_bid_placed
                    s.commit()
            if j.status in ("won", "lost", "capped_out"):
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
    """Re-scrape end times from HiBid for all scheduled snipes using Playwright."""
    import re
    from backend.hibid_scraper import get_browser

    engine = get_engine()
    with Session(engine) as session:
        snipes = session.query(Snipe).filter(Snipe.status.in_(["scheduled", "watching"])).all()
        if not snipes:
            return {"ok": True, "updated": 0}

        browser = await get_browser()
        page = await browser.new_page()
        updated = 0
        days_map = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

        try:
            for snipe in snipes:
                try:
                    await page.goto(snipe.lot_url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(3000)
                    time_el = await page.query_selector('.lot-time-left')
                    if not time_el:
                        continue
                    time_text = await time_el.inner_text()
                    # Parse "Time Remaining: 1d 21h 13m - Sunday 07:10 PM"
                    dash_match = re.search(r'-\s*(\w+)\s+(\d{1,2}):(\d{2})\s*(AM|PM)', time_text, re.IGNORECASE)
                    if not dash_match:
                        continue
                    day_name = dash_match.group(1).lower()
                    hours = int(dash_match.group(2))
                    mins = int(dash_match.group(3))
                    ampm = dash_match.group(4).upper()
                    if ampm == 'PM' and hours != 12:
                        hours += 12
                    if ampm == 'AM' and hours == 12:
                        hours = 0
                    if day_name not in days_map:
                        continue
                    day_idx = days_map.index(day_name)
                    now = datetime.now()
                    today_idx = now.weekday()
                    days_ahead = day_idx - today_idx
                    if days_ahead < 0:
                        days_ahead += 7
                    if days_ahead == 0:
                        candidate = now.replace(hour=hours, minute=mins, second=0, microsecond=0)
                        if candidate <= now:
                            days_ahead = 7
                    from datetime import timedelta
                    end = now.replace(hour=hours, minute=mins, second=0, microsecond=0) + timedelta(days=days_ahead)
                    snipe.end_time = end
                    # Also update in-memory job
                    if snipe.id in active_snipes:
                        active_snipes[snipe.id].end_time = end
                    updated += 1
                    logger.info(f"Snipe {snipe.id}: updated end_time to {end.isoformat()}")
                except Exception as e:
                    logger.warning(f"Snipe {snipe.id}: failed to refresh time: {e}")
            session.commit()
        finally:
            await page.close()

    return {"ok": True, "updated": updated}


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
            .filter(Snipe.status.in_(["won", "lost", "capped_out"]))
            .order_by(Snipe.updated_at.desc())
            .limit(50)
            .all()
        )
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
            "snipes": [
                {
                    "id": s.id,
                    "lot_title": s.lot_title,
                    "lot_url": s.lot_url,
                    "max_cap": s.max_cap,
                    "current_bid": s.current_bid,
                    "status": s.status,
                    "created_at": str(s.created_at),
                }
                for s in snipes
            ],
        }


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
        )
        active_snipes[snipe.id] = job

        async def update_status(j: SnipeJob):
            with Session(engine) as s:
                db_snipe = s.get(Snipe, snipe.id)
                if db_snipe:
                    db_snipe.status = j.status
                    db_snipe.current_bid = getattr(j, 'last_known_price', db_snipe.current_bid)
                    if j.last_bid_placed is not None:
                        db_snipe.our_last_bid = j.last_bid_placed
                    if j.status == "won" and j.last_bid_placed:
                        db_snipe.winning_bid = j.last_bid_placed
                    s.commit()
            if j.status in ("won", "lost", "capped_out"):
                active_snipes.pop(snipe.id, None)

        asyncio.create_task(job.run(on_status_change=update_status))
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


@app.get("/api/cookies/status")
def cookie_status():
    """Check if HiBid cookies are loaded."""
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE) as f:
                cookies = json.load(f)
            return {"has_cookies": True, "count": len(cookies)}
        except Exception:
            pass
    return {"has_cookies": False, "count": 0}


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
