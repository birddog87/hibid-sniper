"""HiBid watchlist: saved searches that auto-scan for lots near you."""

import logging
import math
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.db import get_engine
from backend.hibid_scraper import get_browser
from backend.models import AuctionHouse, Settings, WatchlistResult, WatchlistSearch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL query extracted from HiBid's frontend JS bundle
# ---------------------------------------------------------------------------
LOTSEARCH_QUERY = """
query LotSearch(
  $pageNumber: Int!,
  $pageLength: Int!,
  $searchText: String,
  $zip: String,
  $miles: Int,
  $status: AuctionLotStatus,
  $sortOrder: EventItemSortOrder,
  $shippingOffered: Boolean = false,
  $countryName: String,
  $state: String,
  $category: CategoryId = null,
  $filter: AuctionLotFilter = null,
  $isArchive: Boolean = false,
  $dateStart: DateTime,
  $dateEnd: DateTime,
  $countAsView: Boolean = true,
  $hideGoogle: Boolean = false,
  $auctionId: Int = null,
  $eventItemIds: [Int!] = null
) {
  lotSearch(
    input: {
      auctionId: $auctionId,
      category: $category,
      searchText: $searchText,
      zip: $zip,
      miles: $miles,
      shippingOffered: $shippingOffered,
      countryName: $countryName,
      state: $state,
      status: $status,
      sortOrder: $sortOrder,
      filter: $filter,
      isArchive: $isArchive,
      dateStart: $dateStart,
      dateEnd: $dateEnd,
      countAsView: $countAsView,
      hideGoogle: $hideGoogle,
      eventItemIds: $eventItemIds
    }
    pageNumber: $pageNumber
    pageLength: $pageLength
    sortDirection: DESC
  ) {
    pagedResults {
      pageLength
      pageNumber
      totalCount
      filteredCount
      results {
        id
        itemId
        lead
        description
        lotNumber
        bidAmount
        quantity
        shippingOffered
        pictureCount
        distanceMiles
        featuredPicture {
          thumbnailLocation
          hdThumbnailLocation
          fullSizeLocation
        }
        lotState {
          status
          highBid
          bidCount
          minBid
          timeLeft
          timeLeftSeconds
          timeLeftTitle
          isClosed
          isLive
          reserveSatisfied
        }
        auction {
          id
          eventName
          eventCity
          eventState
          eventZip
          bidCloseDateTime
          currencyAbbreviation
          buyerPremium
          buyerPremiumRate
          auctioneer {
            id
            name
            city
            state
          }
        }
      }
    }
  }
}
""".strip()

KM_PER_MILE = 1.60934

# Reusable page for GraphQL calls — avoids spawning a new Chromium process per search.
_search_page = None


async def _get_search_page():
    """Get or create a single reusable page for watchlist GraphQL calls."""
    global _search_page
    if _search_page is not None:
        try:
            # Check page is still alive
            await _search_page.evaluate("1")
            return _search_page
        except Exception:
            _search_page = None

    browser = await get_browser()
    _search_page = await browser.new_page()
    await _search_page.goto("https://hibid.com", wait_until="domcontentloaded", timeout=20000)
    await _search_page.wait_for_timeout(1500)
    logger.info("Watchlist: created reusable search page")
    return _search_page


async def _close_search_page():
    """Close the reusable search page to free memory after a scan."""
    global _search_page
    if _search_page is not None:
        try:
            await _search_page.close()
        except Exception:
            pass
        _search_page = None
        logger.info("Watchlist: closed reusable search page")


async def search_hibid(
    search_term: str,
    postal_code: str,
    radius_km: int = 50,
    page_number: int = 1,
    page_length: int = 50,
) -> list[dict]:
    """Search HiBid for lots matching *search_term* near *postal_code*.

    Executes the GraphQL query via fetch() inside the Playwright browser
    context so Cloudflare cookies are sent automatically.
    Uses a single reusable page to avoid spawning extra Chromium processes.
    """
    miles = max(1, round(radius_km / KM_PER_MILE))
    variables = {
        "searchText": search_term,
        "zip": postal_code,
        "miles": miles,
        "pageNumber": page_number,
        "pageLength": page_length,
        "status": "OPEN",
        "sortOrder": "TIME_LEFT",
        "countAsView": False,
        "hideGoogle": False,
        "shippingOffered": False,
        "isArchive": False,
        "dateStart": None,
        "dateEnd": None,
    }

    page = await _get_search_page()
    data = await page.evaluate(
        """async ({query, variables}) => {
            try {
                const resp = await fetch('https://hibid.com/graphql', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        operationName: 'LotSearch',
                        query: query,
                        variables: variables
                    }),
                });
                return await resp.json();
            } catch(e) {
                return {error: e.message};
            }
        }""",
        {"query": LOTSEARCH_QUERY, "variables": variables},
    )

    if not data or "error" in data:
        logger.error(f"HiBid search failed for '{search_term}': {data}")
        return []

    try:
        results = data["data"]["lotSearch"]["pagedResults"]["results"]
        total = data["data"]["lotSearch"]["pagedResults"]["totalCount"]
        logger.info(f"HiBid search '{search_term}' near {postal_code}: {total} total, {len(results)} returned")
        return results
    except (KeyError, TypeError) as e:
        logger.error(f"Unexpected search response structure: {e}")
        return []


def _parse_closes_at(lot: dict) -> datetime | None:
    """Derive closing datetime from timeLeftSeconds or timeLeftTitle."""
    state = lot.get("lotState") or {}
    secs = state.get("timeLeftSeconds")
    if secs and secs > 0:
        return datetime.now(timezone.utc) + timedelta(seconds=secs)

    title = state.get("timeLeftTitle") or ""
    # "Internet Bidding closes at: 3/22/2026 9:29:57 PM EST"
    m = re.search(r"closes at:\s*(.+)", title, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        # Strip timezone abbreviation for naive parse, assume ET
        raw_no_tz = re.sub(r"\s+[A-Z]{2,4}$", "", raw)
        try:
            dt = datetime.strptime(raw_no_tz, "%m/%d/%Y %I:%M:%S %p")
            # Assume Eastern Time (UTC-5 standard, UTC-4 DST — close enough)
            return dt.replace(tzinfo=timezone(timedelta(hours=-4)))
        except ValueError:
            pass
    return None


def _lot_url(lot: dict) -> str:
    """Build the HiBid lot URL from a search result."""
    lot_id = lot.get("id") or lot.get("itemId")
    lead = lot.get("lead") or "item"
    slug = re.sub(r"[^a-z0-9]+", "-", lead.lower()).strip("-")
    return f"https://hibid.com/lot/{lot_id}/{slug}"


def _extract_premium_pct(lot: dict) -> float | None:
    """Extract buyer's premium percentage from the auction data."""
    auction = lot.get("auction") or {}
    # buyerPremiumRate is a multiplier (e.g. 1 for no premium? or actual pct?)
    # buyerPremium is a string like "11% on top of Hammer Price"
    bp_str = auction.get("buyerPremium") or ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", bp_str)
    if m:
        return float(m.group(1))
    return None


def match_auction_house(auctioneer_name: str, session: Session, hibid_auctioneer_id: int | None = None) -> int | None:
    """Try to match an auctioneer name to a saved auction house (case-insensitive).

    Also auto-fills the house's auctioneer_id if not already set.
    """
    if not auctioneer_name:
        return None
    houses = session.query(AuctionHouse).all()
    auc_lower = auctioneer_name.lower()
    for h in houses:
        if h.name and h.name.lower() in auc_lower or auc_lower in h.name.lower():
            if hibid_auctioneer_id and not h.auctioneer_id:
                h.auctioneer_id = hibid_auctioneer_id
                logger.info(f"Auto-filled auctioneer_id={hibid_auctioneer_id} for house '{h.name}'")
            return h.id
    return None


async def run_watchlist_scan() -> dict:
    """Run all enabled saved searches and upsert results into the DB.

    Returns summary: {searches_run, new_results, updated_results, errors}.
    """
    engine = get_engine()
    summary = {"searches_run": 0, "new_results": 0, "updated_results": 0, "errors": 0}

    with Session(engine) as session:
        settings = session.get(Settings, 1)
        postal_code = settings.watchlist_postal_code if settings else None
        radius_km = (settings.watchlist_radius_km if settings else None) or 50

        if not postal_code:
            logger.warning("Watchlist scan skipped — no postal code configured")
            return summary

        searches = session.query(WatchlistSearch).filter(WatchlistSearch.enabled == 1).all()
        if not searches:
            logger.info("Watchlist scan skipped — no enabled searches")
            return summary

        search_list = [(s.id, s.search_term) for s in searches]

    # Load always-include auction houses (bypass radius filter).
    with Session(engine) as session:
        always_include_houses = session.query(AuctionHouse).filter(
            AuctionHouse.always_include == 1,
            AuctionHouse.auctioneer_id != None,
        ).all()
        always_include_ids = {h.auctioneer_id for h in always_include_houses}
    if always_include_ids:
        logger.info(f"Always-include auctioneer IDs: {always_include_ids}")

    # Run searches outside the session to avoid long-held locks.
    # Each search_term can contain comma-separated keywords (e.g. "cx9, cx-9")
    # that are searched individually and combined under one group.
    for search_id, term in search_list:
        keywords = [kw.strip() for kw in term.split(",") if kw.strip()]
        lots = []
        seen_lot_ids = set()
        for kw in keywords:
            try:
                kw_lots = await search_hibid(kw, postal_code, radius_km)
                summary["searches_run"] += 1
                for lot in kw_lots:
                    lid = lot.get("itemId") or lot.get("id")
                    if lid and lid not in seen_lot_ids:
                        seen_lot_ids.add(lid)
                        lots.append(lot)
            except Exception as e:
                logger.error(f"Search failed for keyword '{kw}': {e}")
                summary["errors"] += 1

        # Extra search pass for always-include houses outside normal radius.
        # Uses 500 mile radius then filters to only whitelisted auctioneer IDs.
        if always_include_ids:
            for kw in keywords:
                try:
                    wide_lots = await search_hibid(kw, postal_code, radius_km=800)
                    summary["searches_run"] += 1
                    for lot in wide_lots:
                        lid = lot.get("itemId") or lot.get("id")
                        if lid and lid not in seen_lot_ids:
                            auc = (lot.get("auction") or {}).get("auctioneer") or {}
                            if auc.get("id") in always_include_ids:
                                seen_lot_ids.add(lid)
                                lots.append(lot)
                                logger.info(f"Always-include hit: '{lot.get('lead', '')[:50]}' from auctioneer {auc.get('name')}")
                except Exception as e:
                    logger.error(f"Wide search failed for keyword '{kw}': {e}")
                    summary["errors"] += 1

        with Session(engine) as session:
            for lot in lots:
                state = lot.get("lotState") or {}
                auction = lot.get("auction") or {}
                auctioneer = auction.get("auctioneer") or {}
                pic = lot.get("featuredPicture") or {}

                hibid_lot_id = lot.get("itemId") or lot.get("id")
                if not hibid_lot_id:
                    continue

                existing = session.query(WatchlistResult).filter(
                    WatchlistResult.hibid_lot_id == hibid_lot_id
                ).first()

                closes_at = _parse_closes_at(lot)

                # Determine if lot is actually closed: HiBid API flag,
                # timeLeftSeconds <= 0, or closes_at in the past
                now_utc = datetime.now(timezone.utc)
                is_closed = bool(state.get("isClosed"))
                time_left_secs = state.get("timeLeftSeconds")
                if time_left_secs is not None and time_left_secs <= 0:
                    is_closed = True
                if closes_at and closes_at <= now_utc:
                    is_closed = True

                # Use highBid when there are bids, otherwise minBid (starting price).
                # Don't use bidAmount — it's a HiBid placeholder (often 123.45).
                bid_count = state.get("bidCount", 0)
                current_bid = state.get("highBid") if bid_count else state.get("minBid") or 0

                if existing:
                    existing.current_bid = current_bid
                    existing.bid_count = state.get("bidCount", 0)
                    existing.min_bid = state.get("minBid")
                    existing.is_closed = 1 if is_closed else 0
                    existing.last_seen_at = datetime.utcnow()
                    if closes_at:
                        existing.closes_at = closes_at
                    summary["updated_results"] += 1
                else:
                    auctioneer_name = auctioneer.get("name") or ""
                    matched_id = match_auction_house(auctioneer_name, session, hibid_auctioneer_id=auctioneer.get("id"))

                    result = WatchlistResult(
                        search_id=search_id,
                        hibid_lot_id=hibid_lot_id,
                        title=lot.get("lead") or "",
                        lot_url=_lot_url(lot),
                        thumbnail_url=pic.get("thumbnailLocation") or pic.get("hdThumbnailLocation"),
                        current_bid=current_bid,
                        bid_count=state.get("bidCount", 0),
                        min_bid=state.get("minBid"),
                        closes_at=closes_at,
                        is_closed=1 if is_closed else 0,
                        auction_name=auction.get("eventName") or "",
                        auction_city=auction.get("eventCity") or "",
                        auctioneer_name=auctioneer_name,
                        auctioneer_id=auctioneer.get("id"),
                        distance_miles=lot.get("distanceMiles"),
                        buyer_premium_pct=_extract_premium_pct(lot),
                        shipping_offered=1 if lot.get("shippingOffered") else 0,
                        currency=auction.get("currencyAbbreviation") or "CAD",
                        status="new",
                        matched_house_id=matched_id,
                        first_seen_at=datetime.utcnow(),
                        last_seen_at=datetime.utcnow(),
                    )
                    session.add(result)
                    summary["new_results"] += 1

            session.commit()

    # Close the reusable search page to free ~490MB of Chromium memory
    await _close_search_page()

    # Clean up all closed results and results with past closes_at
    now_utc = datetime.utcnow()
    with Session(engine) as session:
        from sqlalchemy import or_
        deleted = session.query(WatchlistResult).filter(
            WatchlistResult.status != "sniped",
            or_(
                WatchlistResult.is_closed == 1,
                WatchlistResult.closes_at <= now_utc,
            ),
        ).delete()
        session.commit()
        if deleted:
            logger.info(f"Cleaned up {deleted} closed/expired watchlist results")

    logger.info(f"Watchlist scan complete: {summary}")
    return summary
