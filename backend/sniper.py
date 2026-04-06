import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from backend.hibid_scraper import get_browser, parse_lot_id_from_url, LotDetails
from backend.calculator import calculate_true_cost
from backend.discord_notify import (
    send_notification, format_snipe_won, format_snipe_lost, format_snipe_capped,
)
from backend.hibid_auth import login_if_needed

logger = logging.getLogger(__name__)

SNIPE_WINDOW_SECONDS = 30
POLL_INTERVAL = 5
SOFT_CLOSE_RECHECK = 2
MAX_SOFT_CLOSE_SECONDS = 1800  # 30 min max in soft-close before giving up
MAX_REASONABLE_PAGE_SECONDS = 12 * 60 * 60
REBID_COOLDOWN_SECONDS = 8
TERMINAL_SNIPE_STATUSES = ("won", "lost", "capped_out", "auth_failed", "error")


def should_bid(current_price: float, max_cap: float, increment: float) -> bool:
    """Return True if placing a bid at current_price + increment stays within max_cap."""
    next_price = current_price + increment
    return next_price <= max_cap


def next_bid_amount(current_price: float, increment: float) -> float:
    """Return the next bid amount given current price and increment."""
    return current_price + increment


def projected_exposure_total(exposure_total: float, previous_commitment: float, new_bid_amount: float) -> float:
    """Project post-bid exposure total by replacing this snipe's current commitment."""
    previous = max(previous_commitment or 0.0, 0.0)
    return float(exposure_total) - previous + float(new_bid_amount)


WAKE_BEFORE_SECONDS = 900  # Wake up 15 minutes before auction end (end times from bookmarklet can be inaccurate)
ESTIMATED_WAKE_BEFORE_SECONDS = 3600  # Wake up 60 minutes early when the queued end time is only an estimate


class SnipeJob:
    def __init__(self, lot_url: str, max_cap: float, premium_pct: float, snipe_id: int | None = None,
                 end_time: datetime | None = None, get_budget=None, log_bid=None,
                 db_our_last_bid: float | None = None, end_time_estimated: bool = False):
        self.lot_url = lot_url
        self.max_cap = max_cap
        self.premium_pct = premium_pct
        self.snipe_id = snipe_id
        self.end_time = end_time
        self.status = "scheduled" if end_time else "watching"
        self.cancelled = False
        self.last_known_price = 0.0
        self.get_budget = get_budget
        self.log_bid = log_bid
        self.last_bid_placed = None
        self.db_our_last_bid = db_our_last_bid  # from DB, survives restarts
        self.end_time_reliable = False
        self.end_time_estimated = end_time_estimated
        self.last_bid_attempt_at = None
        self.last_bid_attempt_amount = None
        self.last_bid_observed_price = None

    def _mark_auth_failed(self, bid_amount: float = 0.0, reason: str = "No auth token available for bidding"):
        logger.error(f"Snipe {self.snipe_id}: {reason}")
        self.status = "auth_failed"
        if self.log_bid:
            self.log_bid(self.snipe_id, bid_amount, "auth_failed", reason)

    def _we_actually_bid(self) -> bool:
        """True if we placed a bid this session OR had one recorded in DB from before restart."""
        return self._our_committed_max() is not None

    def _our_committed_max(self) -> float | None:
        """Highest max bid we know we already committed to HiBid."""
        if self.last_bid_placed is not None:
            return float(self.last_bid_placed)
        if self.db_our_last_bid is not None:
            return float(self.db_our_last_bid)
        return None

    async def run(self, on_status_change=None):
        """Two-phase snipe: sleep until near end, then open browser and monitor."""

        # ── Phase 1: Sleep (no browser) ──
        if self.end_time:
            await self._phase_sleep(on_status_change)
            if self.cancelled:
                if on_status_change:
                    await on_status_change(self)
                return self.status
        else:
            logger.warning(f"Snipe {self.snipe_id}: No end_time, skipping sleep phase")

        # ── Phase 2: Active monitoring (browser open) ──
        self.status = "watching"
        if on_status_change:
            await on_status_change(self)

        await self._phase_active(on_status_change)

        if on_status_change:
            await on_status_change(self)
        return self.status

    async def _phase_sleep(self, on_status_change=None):
        """Phase 1: Sleep in 60s chunks until WAKE_BEFORE_SECONDS before end_time."""
        now = datetime.now(timezone.utc)
        # Ensure end_time is timezone-aware
        end = self.end_time
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        wake_before = self._wake_before_seconds()
        sleep_until = end - timedelta(seconds=wake_before)
        remaining = (sleep_until - now).total_seconds()

        if remaining <= 0:
            logger.info(f"Snipe {self.snipe_id}: End time is within {wake_before}s, skipping sleep")
            return

        logger.info(
            f"Snipe {self.snipe_id}: Sleeping for {remaining:.0f}s "
            f"(waking at {sleep_until.isoformat()}, auction ends {end.isoformat()}, wake_buffer={wake_before}s)"
        )

        while remaining > 0 and not self.cancelled:
            chunk = min(remaining, 60)
            await asyncio.sleep(chunk)
            remaining -= chunk

        if not self.cancelled:
            logger.info(f"Snipe {self.snipe_id}: Waking up — entering active monitoring")

    async def _phase_active(self, on_status_change=None):
        """Phase 2: Open browser, login, poll every 5s, bid at T-3s, handle soft-close."""
        browser = await get_browser()
        page = await browser.new_page()

        try:
            await page.goto(self.lot_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Login before starting snipe
            await login_if_needed(page)
            await page.goto(self.lot_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Extract auth token and auction ID for GraphQL bids
            self._auth_token = await self._get_auth_token(page)
            if self._auth_token:
                logger.info("Got auth token for GraphQL bidding")
                # Validate the token is still accepted by HiBid
                if not await self._validate_token(page):
                    logger.warning(f"Snipe {self.snipe_id}: Auth token expired — attempting cookie refresh")
                    if not await self._refresh_auth(page):
                        self._mark_auth_failed(reason="Auth token rejected by HiBid — session expired, import fresh cookies")
                        if on_status_change:
                            await on_status_change(self)
                        return
                self._auction_id = await self._get_auction_id(page)
                if self._auction_id:
                    logger.info(f"Auction ID: {self._auction_id}")
                    await self._ensure_registered(page)
            else:
                self._mark_auth_failed(reason="No auth token available after login; import fresh HiBid cookies")
                if on_status_change:
                    await on_status_change(self)
                return

            # Refresh end_time from the live page — bookmarklet estimates can be off by hours
            # Extract twice with a gap to validate (timeLeftSeconds can return wrong-lot data)
            live_end_1 = await self._extract_end_time_from_page(page)
            if live_end_1:
                await asyncio.sleep(3)
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                live_end_2 = await self._extract_end_time_from_page(page)
                if live_end_2:
                    live_end, self.end_time_reliable = self._choose_live_end_time(live_end_1, live_end_2)
                else:
                    live_end = live_end_1
                    self.end_time_reliable = False
                if self.end_time_reliable:
                    self.end_time_estimated = False
                self.end_time = live_end
                logger.info(f"Snipe {self.snipe_id}: end_time set to {live_end.isoformat()}")
                if on_status_change:
                    await on_status_change(self)

            consecutive_failures = 0
            MAX_FAILURES_BEFORE_RELOAD = 3
            soft_close_entered_at = None  # Track when we entered soft-close
            _last_api_check = 0  # timestamp of last HTTP-based timer check
            API_CHECK_INTERVAL = 30  # seconds between HTTP timer refreshes
            logger.info(f"Snipe {self.snipe_id}: Entering poll loop")

            while not self.cancelled and self.status not in TERMINAL_SNIPE_STATUSES:
                state = await self._get_auction_state(page)
                if state is None:
                    consecutive_failures += 1
                    logger.error(f"Snipe {self.snipe_id}: Could not read auction state (attempt {consecutive_failures})")
                    if consecutive_failures >= MAX_FAILURES_BEFORE_RELOAD:
                        logger.warning(f"Snipe {self.snipe_id}: {consecutive_failures} consecutive failures, reloading page")
                        try:
                            await page.goto(self.lot_url, wait_until="domcontentloaded", timeout=30000)
                            await page.wait_for_timeout(2000)
                        except Exception as e:
                            logger.error(f"Snipe {self.snipe_id}: Page reload failed: {e}")
                        consecutive_failures = 0
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                consecutive_failures = 0

                current_price = state["current_price"]
                increment = state["increment"]
                page_seconds = state["seconds_left"]
                is_ended = state["is_ended"]
                self.last_known_price = current_price
                self.increment = increment

                seconds_left = self._compute_seconds_left(page_seconds)

                # Every 30s, cross-check our wall clock with a fresh HTTP fetch
                # This catches wrong-lot timeLeftSeconds without relying on frozen page timer
                import time as _time
                now_ts = _time.time()
                if now_ts - _last_api_check > API_CHECK_INTERVAL and seconds_left > 60:
                    _last_api_check = now_ts
                    try:
                        from backend.hibid_api import get_lot_status_via_html
                        api_state = await get_lot_status_via_html(self.lot_url)
                        if api_state:
                            if api_state["is_ended"]:
                                is_ended = True
                                state["is_ended"] = True
                                logger.info(f"Snipe {self.snipe_id}: HTTP check says auction ended")
                            elif api_state["seconds_left"] is not None:
                                api_end = datetime.now(timezone.utc) + timedelta(seconds=api_state["seconds_left"])
                                if self.end_time:
                                    end_aware = self.end_time.replace(tzinfo=timezone.utc) if self.end_time.tzinfo is None else self.end_time
                                    drift = abs((api_end - end_aware).total_seconds())
                                    if drift > 60:
                                        self.end_time = api_end
                                        seconds_left = api_state["seconds_left"]
                                        logger.warning(f"Snipe {self.snipe_id}: HTTP timer disagrees by {drift:.0f}s — corrected end_time to {api_end.isoformat()}")
                                    else:
                                        logger.info(f"Snipe {self.snipe_id}: HTTP timer confirms clock (drift {drift:.0f}s)")
                                else:
                                    self.end_time = api_end
                                    seconds_left = api_state["seconds_left"]
                            if api_state["current_price"] is not None and api_state["current_price"] > current_price:
                                current_price = api_state["current_price"]
                                self.last_known_price = current_price
                                state["current_price"] = current_price
                    except Exception as e:
                        logger.debug(f"Snipe {self.snipe_id}: HTTP timer check failed: {e}")

                logger.info(f"Snipe {self.snipe_id}: price=${current_price} clock_secs={seconds_left:.0f} page_secs={page_seconds} ended={is_ended} time='{state.get('time_left', '')[:50]}'")

                # Update DB with current price on every poll
                if on_status_change:
                    await on_status_change(self)

                if is_ended:
                    # Final price fetch — the page price may be stale if the user
                    # bid manually.  Do one last HTTP scrape to get the real sold price.
                    try:
                        from backend.hibid_api import get_lot_status_via_html
                        final_state = await get_lot_status_via_html(self.lot_url)
                        if final_state and final_state.get("current_price"):
                            final_price = final_state["current_price"]
                            if final_price > current_price:
                                logger.info(f"Snipe {self.snipe_id}: Final HTTP price ${final_price} > page price ${current_price} (user may have bid manually)")
                                current_price = final_price
                                self.last_known_price = current_price
                    except Exception as e:
                        logger.debug(f"Snipe {self.snipe_id}: Final price fetch failed: {e}")

                    # Only trust "we_are_winning" page text if we actually placed a bid.
                    # HiBid pages show "high bidder" / "your max" text generically on
                    # closed lots even if WE never bid.  Without this gate the bot
                    # marks lots as "won" that it never touched.
                    actually_won = state.get("we_are_winning") and self._we_actually_bid()
                    if actually_won:
                        self.status = "won"
                        cost = calculate_true_cost(current_price, self.premium_pct)
                        await send_notification(format_snipe_won(
                            lot_title=state.get("title", "Unknown"),
                            lot_url=self.lot_url,
                            winning_bid=current_price,
                            true_cost=cost["total"],
                        ))
                    else:
                        self.status = "lost"
                        await send_notification(format_snipe_lost(
                            lot_title=state.get("title", "Unknown"),
                            lot_url=self.lot_url,
                            final_price=current_price,
                            your_cap=self.max_cap,
                        ))
                    break

                if not should_bid(current_price, self.max_cap, increment):
                    # If we're winning at this price, don't cap out — keep monitoring
                    # until the auction actually ends so we can mark it as "won"
                    if state.get("we_are_winning") and self._we_actually_bid():
                        logger.info(f"Snipe {self.snipe_id}: At cap (${current_price}/${self.max_cap}) but winning — holding...")
                        await asyncio.sleep(SOFT_CLOSE_RECHECK)
                        continue
                    self.status = "capped_out"
                    await send_notification(format_snipe_capped(
                        lot_title=state.get("title", "Unknown"),
                        lot_url=self.lot_url,
                        current_price=current_price,
                        your_cap=self.max_cap,
                    ))
                    break

                # SOFT-CLOSE HANDLING: past our stored end_time but auction still active
                # In soft-close, the auction can end ANY second — bid immediately if able
                if seconds_left < 0 and self.end_time:
                    now = datetime.now(timezone.utc)
                    if soft_close_entered_at is None:
                        soft_close_entered_at = now
                        logger.info(f"Snipe {self.snipe_id}: Entering soft-close mode")

                    soft_close_elapsed = (now - soft_close_entered_at).total_seconds()
                    logger.info(f"Snipe {self.snipe_id}: SOFT CLOSE — past end_time by {-seconds_left:.0f}s, page_secs={page_seconds}, sc_elapsed={soft_close_elapsed:.0f}s")

                    # Bail out if we've been in soft-close too long — auction is gone
                    if soft_close_elapsed > MAX_SOFT_CLOSE_SECONDS:
                        logger.warning(f"Snipe {self.snipe_id}: Soft-close timeout ({MAX_SOFT_CLOSE_SECONDS}s) — treating as ended")
                        self.status = "lost"
                        await send_notification(format_snipe_lost(
                            lot_title=state.get("title", "Unknown"),
                            lot_url=self.lot_url,
                            final_price=current_price,
                            your_cap=self.max_cap,
                        ))
                        break

                    # Don't bid if we're already winning — but only trust this if we actually bid
                    if state.get("we_are_winning") and self._we_actually_bid():
                        logger.info(f"Snipe {self.snipe_id}: Already winning at ${current_price} during soft close, waiting...")
                        await asyncio.sleep(SOFT_CLOSE_RECHECK)
                        continue

                    # We're in soft-close and NOT winning — bid NOW
                    self.status = "bidding"
                    if on_status_change:
                        await on_status_change(self)

                    bid_amount = next_bid_amount(current_price, increment)
                    if not self._should_submit_bid(bid_amount, current_price, page_seconds):
                        await asyncio.sleep(SOFT_CLOSE_RECHECK)
                        continue
                    logger.info(f"Snipe {self.snipe_id}: SOFT CLOSE BID at ${bid_amount}")
                    success = await self._place_bid(page, bid_amount)

                    # If auth failed, abort immediately — no point looping
                    if self.status == "auth_failed":
                        logger.error(f"Snipe {self.snipe_id}: Auth failed during soft-close, aborting")
                        break

                    if success:
                        await asyncio.sleep(SOFT_CLOSE_RECHECK)
                        await page.reload(wait_until="domcontentloaded")
                        await page.wait_for_timeout(2000)
                    else:
                        await asyncio.sleep(SOFT_CLOSE_RECHECK)
                    continue

                # If time parse failed and no clock, wait and retry
                if seconds_left < 0 and not self.end_time:
                    logger.warning(f"Snipe {self.snipe_id}: Could not parse time from '{state.get('time_left', '')}', waiting...")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                if seconds_left > SNIPE_WINDOW_SECONDS:
                    wait_time = min(seconds_left - SNIPE_WINDOW_SECONDS, POLL_INTERVAL)
                    await asyncio.sleep(wait_time)
                    continue

                # Don't bid if we're already winning — avoid bidding against ourselves
                # Only trust page text if we actually placed a bid
                if state.get("we_are_winning") and self._we_actually_bid():
                    logger.info(f"Snipe {self.snipe_id}: Already winning at ${current_price}, waiting...")
                    await asyncio.sleep(SOFT_CLOSE_RECHECK)
                    continue

                # Within snipe window - place the bid
                self.status = "bidding"
                if on_status_change:
                    await on_status_change(self)

                bid_amount = next_bid_amount(current_price, increment)
                if not self._should_submit_bid(bid_amount, current_price, page_seconds):
                    await asyncio.sleep(SOFT_CLOSE_RECHECK)
                    continue
                logger.info(f"Sniping {self.lot_url} at ${bid_amount}")
                success = await self._place_bid(page, bid_amount)

                # If auth failed, abort immediately
                if self.status == "auth_failed":
                    logger.error(f"Snipe {self.snipe_id}: Auth failed during bid, aborting")
                    break

                if success:
                    await asyncio.sleep(SOFT_CLOSE_RECHECK)
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
                else:
                    logger.warning("Bid placement may have failed, rechecking...")
                    await asyncio.sleep(SOFT_CLOSE_RECHECK)

        finally:
            await page.close()

    async def _get_auction_state(self, page) -> dict | None:
        """Extract current auction state from the page using real HiBid selectors."""
        try:
            state = await page.evaluate("""() => {
                const getText = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? el.innerText.trim() : null;
                };
                return {
                    // Real HiBid selectors:
                    // .lot-high-bid -> "High Bid: 16.00 CAD"
                    current_price: getText('.lot-high-bid'),
                    // .lot-bid-button -> "Bid 18.00 CAD" (next bid amount!)
                    bid_button: getText('.lot-bid-button'),
                    // .lot-time-left -> "Time Remaining: 54m 43s - Friday 04:00 PM"
                    time_left: getText('.lot-time-left'),
                    // h1 -> "Lot # : 1 - Title Here"
                    title: getText('h1'),
                    // .lot-bid-history -> "8 Bids"
                    bid_history: getText('.lot-bid-history'),
                };
            }""")

            # Check for ended state BEFORE requiring current_price
            # When auction closes, bid button and price disappear but time_left shows "Bidding Closed"
            time_text = state.get("time_left") or ""
            if any(kw in time_text.lower() for kw in ("closed", "ended", "sold", "closing")):
                # Auction is over — try to get price, default to last known
                price_text = state.get("current_price") or ""
                price_match = re.search(r"([\d,]+\.?\d*)", price_text)
                price = float(price_match.group(1).replace(",", "")) if price_match else self.last_known_price

                raw_title = state.get("title") or "Unknown"
                title = re.sub(r"^Lot\s*#\s*:\s*\d+\s*-\s*", "", raw_title).strip()

                we_are_winning = False
                try:
                    winning_indicator = await page.evaluate("""() => {
                        const body = document.body.innerText.toLowerCase();
                        return body.includes('high bidder') ||
                               body.includes('may have won') ||
                               body.includes('you won') ||
                               body.includes('your max');
                    }""")
                    we_are_winning = bool(winning_indicator)
                except Exception:
                    pass

                return {
                    "current_price": price,
                    "increment": 0,
                    "seconds_left": 0,
                    "is_ended": True,
                    "title": title,
                    "we_are_winning": we_are_winning,
                    "time_left": time_text,
                }

            if not state or not state.get("current_price"):
                # If all elements are gone AND we're past end_time, the auction is over
                # HiBid strips the entire bidding UI some time after close
                if self.end_time:
                    end = self.end_time
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > end + timedelta(minutes=5):
                        raw_title = (state or {}).get("title") or "Unknown"
                        title = re.sub(r"^Lot\s*#\s*:\s*\d+\s*-\s*", "", raw_title).strip()
                        logger.info(f"Snipe {self.snipe_id}: Page elements gone and past end_time — treating as ended")
                        return {
                            "current_price": self.last_known_price,
                            "increment": 0,
                            "seconds_left": 0,
                            "is_ended": True,
                            "title": title,
                            "we_are_winning": False,
                            "time_left": "",
                        }
                page_url = page.url
                logger.warning(f"Snipe {self.snipe_id}: _get_auction_state got nothing. URL={page_url}, raw={state}")
                return None

            # Parse "High Bid: 16.00 CAD" -> 16.00
            price_match = re.search(r"([\d,]+\.?\d*)", state["current_price"])
            price = float(price_match.group(1).replace(",", "")) if price_match else 0

            # Parse increment from bid button: "Bid 18.00 CAD" -> 18.00 - current
            bid_btn_text = state.get("bid_button") or ""
            btn_match = re.search(r"([\d,]+\.?\d*)", bid_btn_text)
            next_bid_price = float(btn_match.group(1).replace(",", "")) if btn_match else 0
            increment = (next_bid_price - price) if next_bid_price > price else 5.0

            # Parse time: "Time Remaining: 54m 43s - Friday 04:00 PM"
            time_text = state.get("time_left") or ""
            seconds_left = self._parse_time_remaining(time_text)

            # Check if ended — keyword match is authoritative regardless of parsed time
            ended_keywords = ("ended" in time_text.lower()
                              or "closed" in time_text.lower()
                              or "closing" in time_text.lower()
                              or "sold" in time_text.lower())
            is_ended = ended_keywords or (seconds_left == 0)

            # Clean title: "Lot # : 1 - Something" -> "Something"
            raw_title = state.get("title") or "Unknown"
            title = re.sub(r"^Lot\s*#\s*:\s*\d+\s*-\s*", "", raw_title).strip()

            # Detect if we're the winning bidder
            # HiBid shows "high bidder" during auction, "May Have Won" / "Your Max" after close
            we_are_winning = False
            try:
                winning_indicator = await page.evaluate("""() => {
                    const body = document.body.innerText.toLowerCase();
                    return body.includes('high bidder') ||
                           body.includes('may have won') ||
                           body.includes('you won') ||
                           body.includes('your max');
                }""")
                we_are_winning = bool(winning_indicator)
            except Exception:
                pass

            return {
                "current_price": price,
                "increment": increment,
                "seconds_left": seconds_left,
                "is_ended": is_ended,
                "title": title,
                "we_are_winning": we_are_winning,
            }
        except Exception as e:
            logger.error(f"Error reading auction state: {e}")
            return None

    async def _extract_end_time_from_page(self, page) -> datetime | None:
        """Extract the exact auction close time from HiBid's embedded Apollo SSR state.
        Returns a timezone-aware UTC datetime, or None if extraction fails."""
        try:
            result = await page.evaluate("""() => {
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent;
                    if (!text.includes('timeLeftTitle')) continue;
                    // Prefer timeLeftSeconds — timezone-immune countdown
                    // (HiBid often mislabels EDT as EST, causing 1-hour errors with absolute times)
                    const secsMatch = text.match(/"timeLeftSeconds"\\s*:\\s*([\\d.]+)/);
                    if (secsMatch) return { type: 'seconds', value: parseFloat(secsMatch[1]) };
                    // Fallback: absolute close time string
                    const titleMatch = text.match(/"timeLeftTitle"\\s*:\\s*"Internet Bidding closes at:\\s*([^"]+)"/);
                    if (titleMatch) return { type: 'absolute', value: titleMatch[1].trim() };
                    break;
                }
                // Strategy 3: parse from DOM time element
                const timeEl = document.querySelector('.lot-time-left');
                const timeText = timeEl ? timeEl.innerText.trim() : '';
                if (timeText) return { type: 'dom', value: timeText };
                return null;
            }""")

            if not result:
                logger.info(f"Snipe {self.snipe_id}: Could not extract end time from page")
                return None

            if result["type"] == "absolute":
                # Parse "M/D/YYYY H:MM:SS AM/PM TZ"
                close_str = result["value"]
                m = re.match(
                    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)\s*(\w+)?",
                    close_str, re.IGNORECASE
                )
                if m:
                    hrs = int(m.group(4))
                    mins = int(m.group(5))
                    secs = int(m.group(6) or 0)
                    ampm = m.group(7).upper()
                    tz_abbr = (m.group(8) or "").upper()
                    if ampm == "PM" and hrs != 12: hrs += 12
                    if ampm == "AM" and hrs == 12: hrs = 0
                    tz_offsets = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}
                    offset_hrs = tz_offsets.get(tz_abbr, -4)  # default EDT
                    dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), hrs, mins, secs, tzinfo=timezone.utc)
                    dt = dt - timedelta(hours=offset_hrs)  # convert local to UTC
                    logger.info(f"Snipe {self.snipe_id}: Extracted absolute end time: {dt.isoformat()} (from '{close_str}')")
                    return dt

            if result["type"] == "seconds":
                secs_left = result["value"]
                if secs_left > 0:
                    dt = datetime.now(timezone.utc) + timedelta(seconds=secs_left)
                    logger.info(f"Snipe {self.snipe_id}: Extracted end time from timeLeftSeconds: {dt.isoformat()} ({secs_left:.0f}s from now)")
                    return dt

            if result["type"] == "dom":
                # Parse countdown from DOM text
                text = result["value"]
                total_secs = 0
                for pat, mult in [(r"(\d+)\s*d", 86400), (r"(\d+)\s*h", 3600), (r"(\d+)\s*m(?!a)", 60), (r"(\d+)\s*s", 1)]:
                    match = re.search(pat, text)
                    if match:
                        total_secs += int(match.group(1)) * mult
                if total_secs > 0:
                    dt = datetime.now(timezone.utc) + timedelta(seconds=total_secs)
                    logger.info(f"Snipe {self.snipe_id}: Extracted end time from DOM countdown: {dt.isoformat()} ({total_secs}s from now)")
                    return dt

        except Exception as e:
            logger.warning(f"Snipe {self.snipe_id}: Failed to extract end time from page: {e}")
        return None

    def _parse_time_remaining(self, text: str) -> float:
        """Parse a time string like '54m 43s' or '2d 5h 30m 10s' into total seconds.
        Returns -1 if no time components found (parse failure) to avoid
        accidentally triggering the snipe window."""
        total = 0
        found_any = False
        days = re.search(r"(\d+)\s*d", text)
        hours = re.search(r"(\d+)\s*h", text)
        minutes = re.search(r"(\d+)\s*m(?!a)", text)  # 'm' but not 'market' etc
        seconds = re.search(r"(\d+)\s*s", text)
        if days:
            total += int(days.group(1)) * 86400
            found_any = True
        if hours:
            total += int(hours.group(1)) * 3600
            found_any = True
        if minutes:
            total += int(minutes.group(1)) * 60
            found_any = True
        if seconds:
            total += int(seconds.group(1))
            found_any = True
        if not found_any:
            return -1  # Could not parse — caller should treat as unknown
        return total

    def _choose_live_end_time(self, live_end_1: datetime, live_end_2: datetime) -> tuple[datetime, bool]:
        """Choose the safer end-time candidate.

        When the live page disagrees with itself, bias earlier rather than later.
        Early bids are recoverable in a soft-close auction; late bids are not.
        """
        drift_between = abs((live_end_1 - live_end_2).total_seconds())
        if drift_between < 30:
            chosen = min(live_end_1, live_end_2)
            logger.info(
                f"Snipe {self.snipe_id}: end_time validated (two reads agree within {drift_between:.0f}s): "
                f"{chosen.isoformat()}"
            )
            return chosen, True

        chosen = min(live_end_1, live_end_2)
        logger.warning(
            f"Snipe {self.snipe_id}: end_time reads disagree by {drift_between:.0f}s — "
            f"using earlier safer value: {chosen.isoformat()}"
        )
        return chosen, False

    def _wake_before_seconds(self) -> int:
        if self.end_time_estimated and not self.end_time_reliable:
            return ESTIMATED_WAKE_BEFORE_SECONDS
        return WAKE_BEFORE_SECONDS

    def _compute_seconds_left(self, page_seconds: float) -> float:
        """Combine wall clock and page timer, always biasing toward the more urgent clock."""
        clock_seconds = None
        if self.end_time:
            end = self.end_time
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            clock_seconds = (end - datetime.now(timezone.utc)).total_seconds()

        page_seconds_valid = None
        if 0 < page_seconds <= MAX_REASONABLE_PAGE_SECONDS:
            page_seconds_valid = page_seconds

        candidates = [value for value in (clock_seconds, page_seconds_valid) if value is not None]
        if candidates:
            return min(candidates)
        return -1

    def _should_submit_bid(self, bid_amount: float, current_price: float, page_seconds: float) -> bool:
        """Prevent duplicate or self-outbidding bids when the page state is stale."""
        now = datetime.now(timezone.utc)
        committed_max = self._our_committed_max()

        if committed_max is not None and current_price <= committed_max:
            logger.info(
                f"Snipe {self.snipe_id}: Holding at ${current_price:.2f}; "
                f"already committed up to ${committed_max:.2f} (next ${bid_amount:.2f}, page_secs={page_seconds})"
            )
            return False

        if self.last_bid_attempt_at is None:
            return True

        elapsed = (now - self.last_bid_attempt_at).total_seconds()
        same_amount = self.last_bid_attempt_amount == bid_amount
        same_price = self.last_bid_observed_price == current_price

        if same_amount and same_price and elapsed < REBID_COOLDOWN_SECONDS:
            logger.info(
                f"Snipe {self.snipe_id}: Suppressing duplicate bid ${bid_amount:.2f} "
                f"(price still ${current_price:.2f}, page_secs={page_seconds}, cooldown {elapsed:.1f}s)"
            )
            return False
        return True

    def _bid_allowed(self, bid_amount: float) -> bool:
        """Check snipe cap and budget guardrails before submitting a bid."""
        if bid_amount > self.max_cap:
            logger.warning(
                f"[CAP BLOCKED] Snipe {self.snipe_id}: bid ${bid_amount:.2f} exceeds cap ${self.max_cap:.2f}"
            )
            if self.log_bid:
                self.log_bid(self.snipe_id, bid_amount, "cap_blocked", f"max_cap=${self.max_cap:.2f}")
            return False

        if not self.get_budget:
            return True

        budget = self.get_budget()
        hard_max = float(budget.get("emergency_bid_hard_max", 0.0) or 0.0)
        if hard_max > 0 and bid_amount > hard_max:
            logger.warning(
                f"[CAP BLOCKED] Snipe {self.snipe_id}: bid ${bid_amount:.2f} exceeds emergency hard max "
                f"${hard_max:.2f}"
            )
            if self.log_bid:
                self.log_bid(
                    self.snipe_id,
                    bid_amount,
                    "cap_blocked",
                    f"emergency_bid_hard_max=${hard_max:.2f}",
                )
            return False

        if budget["global_spend_cap"] == 0:
            logger.warning(
                f"[BUDGET BLOCKED] Snipe {self.snipe_id}: global cap not configured, bid blocked."
            )
            if self.log_bid:
                self.log_bid(self.snipe_id, bid_amount, "budget_blocked", "global cap unset")
            return False

        if bid_amount > budget["max_single_snipe_cap"]:
            logger.warning(
                f"[CAP BLOCKED] Snipe {self.snipe_id}: bid ${bid_amount:.2f} exceeds single cap "
                f"${budget['max_single_snipe_cap']:.2f}"
            )
            if self.log_bid:
                self.log_bid(
                    self.snipe_id,
                    bid_amount,
                    "cap_blocked",
                    f"max_single_snipe_cap=${budget['max_single_snipe_cap']:.2f}",
                )
            return False

        if bid_amount > budget["remaining"]:
            logger.warning(
                f"[BUDGET BLOCKED] Snipe {self.snipe_id}: bid ${bid_amount:.2f} blocked. "
                f"Remaining: ${budget['remaining']:.2f}"
            )
            if self.log_bid:
                self.log_bid(
                    self.snipe_id,
                    bid_amount,
                    "budget_blocked",
                    f"remaining=${budget['remaining']:.2f}",
                )
            return False

        current_commitment = self._our_committed_max()
        if current_commitment is None:
            current_commitment = self.last_known_price
        projected = projected_exposure_total(
            budget.get("exposure_total", 0.0),
            current_commitment,
            bid_amount,
        )
        if projected > budget["global_spend_cap"]:
            logger.warning(
                f"[EXPOSURE BLOCKED] Snipe {self.snipe_id}: projected ${projected:.2f} exceeds "
                f"cap ${budget['global_spend_cap']:.2f}"
            )
            if self.log_bid:
                self.log_bid(
                    self.snipe_id,
                    bid_amount,
                    "budget_blocked",
                    f"projected_exposure=${projected:.2f}",
                )
            return False

        return True

    def _record_successful_bid(self, bid_amount: float, status: str, message: str = ""):
        self.last_bid_placed = bid_amount
        if self.log_bid:
            detail = status if not message else f"{status} {message}"
            self.log_bid(self.snipe_id, bid_amount, "placed", detail.strip())

    def _record_bid_attempt(self, bid_amount: float, current_price: float):
        self.last_bid_attempt_at = datetime.now(timezone.utc)
        self.last_bid_attempt_amount = bid_amount
        self.last_bid_observed_price = current_price

    async def _place_bid(self, page, bid_amount: float = None) -> bool:
        """Place a bid via HiBid's GraphQL API."""
        if not hasattr(self, '_auth_token') or not self._auth_token:
            self._mark_auth_failed(bid_amount or 0.0)
            return False

        lot_id = int(parse_lot_id_from_url(self.lot_url))

        # If no explicit amount, read it from the bid button
        if bid_amount is None:
            bid_btn_text = await page.evaluate("""() => {
                const el = document.querySelector('.lot-bid-button');
                return el ? el.innerText.trim() : null;
            }""")
            if bid_btn_text:
                match = re.search(r"([\d,]+\.?\d*)", bid_btn_text)
                bid_amount = float(match.group(1).replace(",", "")) if match else None
            if bid_amount is None:
                logger.error("Could not determine bid amount")
                return False

        try:
            current_amount = bid_amount
            for attempt in range(3):
                if not self._bid_allowed(current_amount):
                    return False

                observed_price = self.last_known_price
                self._record_bid_attempt(current_amount, observed_price)

                result = await asyncio.wait_for(page.evaluate("""async (args) => {
                    const [token, lotId, bidAmount] = args;
                    const controller = new AbortController();
                    setTimeout(() => controller.abort(), 10000);
                    const resp = await fetch('https://hibid.com/graphql', {
                        signal: controller.signal,
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'Authorization': 'Bearer ' + token
                        },
                        body: JSON.stringify({
                            operationName: 'LotBid',
                            variables: {
                                lotId: lotId,
                                bidAmount: bidAmount,
                                reConfirmed: true
                            },
                            query: `mutation LotBid($lotId: Int!, $bidAmount: Decimal!, $reConfirmed: Boolean!) {
                                bid(input: {lotId: $lotId, bidAmount: $bidAmount, reConfirmed: $reConfirmed}) {
                                    __typename
                                    ... on BidResultType {
                                        bidStatus
                                        suggestedBid
                                        bidMessage
                                    }
                                    ... on InvalidInputError {
                                        messages
                                        errors { fieldName messages }
                                    }
                                }
                            }`
                        })
                    });
                    const status = resp.status;
                    const body = await resp.json();
                    return { httpStatus: status, ...body };
                }""", [self._auth_token, lot_id, current_amount]), timeout=15)

                logger.info(f"Snipe {self.snipe_id}: GraphQL response: {str(result)[:200]}")

                # Detect auth failures — HTTP 401/403 or GraphQL errors mentioning auth
                http_status = result.get("httpStatus", 200)
                gql_errors = result.get("errors", [])
                if http_status in (401, 403) or any("auth" in str(e).lower() or "unauthorized" in str(e).lower() for e in gql_errors):
                    logger.error(f"Snipe {self.snipe_id}: Auth failure during bid (HTTP {http_status}). Attempting token refresh...")
                    refreshed = await self._refresh_auth(page)
                    if refreshed:
                        continue  # retry the bid with fresh token
                    self._mark_auth_failed(current_amount, "Auth expired during bidding")
                    return False

                bid_data = result.get("data", {}).get("bid", {})
                typename = bid_data.get("__typename", "")

                if typename == "BidResultType":
                    status = str(bid_data.get("bidStatus", "") or "").strip().upper()
                    message = str(bid_data.get("bidMessage", "") or "").strip()
                    message_key = message.lower()
                    suggested_raw = bid_data.get("suggestedBid")
                    suggested_bid = float(suggested_raw) if suggested_raw not in (None, "") else None
                    logger.info(f"Bid result: status={status}, message={message}")

                    if status in ("WINNING", "OUTBID", "ACCEPTED"):
                        detail = message or (f"suggested=${suggested_bid:.2f}" if status == "OUTBID" and suggested_bid else "")
                        self._record_successful_bid(current_amount, status, detail)
                        return True

                    if status == "NO_BID" and message_key == "previousmaxbid":
                        self._record_successful_bid(current_amount, "ACCEPTED", "PreviousMaxBid")
                        return True

                    if status == "NO_BID" and message_key == "registerfirst":
                        logger.warning(f"Snipe {self.snipe_id}: RegisterFirst from bid API, retrying after registration")
                        if await self._ensure_registered(page):
                            continue
                        if self.log_bid:
                            self.log_bid(self.snipe_id, current_amount, "error", "Not registered")
                        return False

                    if status == "NO_BID" and message_key == "increasebid" and suggested_bid and suggested_bid > current_amount:
                        logger.info(
                            f"Snipe {self.snipe_id}: Bid ${current_amount:.2f} stale; retrying with suggested ${suggested_bid:.2f}"
                        )
                        current_amount = suggested_bid
                        continue

                    logger.warning(f"Unexpected bid status: {status} - {message}")
                    return status not in ("NO_BID",)

                if typename == "InvalidInputError":
                    messages = bid_data.get("messages", [])
                    logger.error(f"Bid rejected: {messages}")
                    if self.log_bid:
                        self.log_bid(self.snipe_id, current_amount, "error", f"Rejected: {messages}")
                    return False

                logger.error(f"Unexpected response: {result}")
                return False

            logger.warning(f"Snipe {self.snipe_id}: Gave up after repeated bid retries")
            return False

        except asyncio.TimeoutError:
            logger.error(f"Snipe {self.snipe_id}: GraphQL bid TIMED OUT after 15s — page may be stale, reloading")
            try:
                await page.goto(self.lot_url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
            except Exception:
                pass
            if self.log_bid:
                self.log_bid(self.snipe_id, bid_amount or 0, "error", "Bid timed out")
            return False
        except Exception as e:
            logger.error(f"Snipe {self.snipe_id}: Error placing GraphQL bid: {e}")
            if self.log_bid:
                self.log_bid(self.snipe_id, bid_amount or 0, "error", str(e))
            return False

    async def _refresh_auth(self, page) -> bool:
        """Attempt to refresh auth by reloading cookies from file and re-extracting token."""
        try:
            cookie_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "hibid_cookies.json")
            if not os.path.exists(cookie_path):
                return False
            with open(cookie_path) as f:
                saved_cookies = json.load(f)
            from backend.hibid_scraper import inject_cookies
            await inject_cookies(saved_cookies)
            await page.goto(self.lot_url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            new_token = await self._get_auth_token(page)
            if new_token and await self._validate_token(page):
                self._auth_token = new_token
                logger.info(f"Snipe {self.snipe_id}: Auth refreshed successfully mid-bid")
                return True
            logger.warning(f"Snipe {self.snipe_id}: Auth refresh failed — token still invalid")
        except Exception as e:
            logger.error(f"Snipe {self.snipe_id}: Auth refresh error: {e}")
        return False

    async def _get_auth_token(self, page) -> str | None:
        """Extract JWT auth token, trying multiple sources.

        Priority:
        1. document.cookie (fastest, works if cookie is live)
        2. Playwright context cookies (sees httpOnly cookies)
        3. Saved cookie file (works even if browser-level expiry passed,
           since the JWT itself may still be valid server-side)
        """
        # 1. Try document.cookie
        try:
            token = await page.evaluate("""() => {
                const cookies = document.cookie.split(';').map(c => c.trim());
                const session = cookies.find(c => c.startsWith('sessionId='));
                return session ? session.split('=').slice(1).join('=') : null;
            }""")
            if token:
                return token
        except Exception:
            pass

        # 2. Try Playwright context cookies (survives httpOnly)
        try:
            all_cookies = await page.context.cookies(["https://hibid.com", "https://www.hibid.com"])
            for c in all_cookies:
                if c["name"] == "sessionId":
                    logger.info("Got auth token from Playwright context cookies")
                    return c["value"]
        except Exception:
            pass

        # 3. Fallback: read raw value from saved cookie file (ignores expiry)
        try:
            from backend.hibid_api import get_auth_token as get_file_token
            token = get_file_token()
            if token:
                logger.info("Got auth token from saved cookie file (cookie may be expired in browser)")
                return token
        except Exception:
            pass

        return None

    async def _validate_token(self, page) -> bool:
        """Check if the auth token is actually valid by making a lightweight GraphQL call."""
        try:
            result = await page.evaluate("""async (token) => {
                try {
                    const resp = await fetch('https://hibid.com/graphql', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'Authorization': 'Bearer ' + token
                        },
                        body: JSON.stringify({
                            query: '{ buyerPayInfo { ... on BuyerPayInfo { id } } }'
                        })
                    });
                    if (resp.status === 401 || resp.status === 403) return false;
                    const data = await resp.json();
                    // If we get errors about auth/unauthorized, token is dead
                    if (data.errors) {
                        const msg = JSON.stringify(data.errors).toLowerCase();
                        if (msg.includes('unauthorized') || msg.includes('not authenticated'))
                            return false;
                    }
                    return true;
                } catch(e) { return true; }  // network error, assume token ok
            }""", self._auth_token)
            return bool(result)
        except Exception:
            return True  # Don't block on validation errors

    async def _get_auction_id(self, page) -> int | None:
        """Extract auction ID from the page's Apollo cache."""
        try:
            auction_id = await page.evaluate(r"""() => {
                const scripts = document.querySelectorAll('script:not([src])');
                for (const s of scripts) {
                    const match = s.textContent.match(/"Auction:(\d+)"/);
                    if (match) return parseInt(match[1]);
                }
                return null;
            }""")
            return auction_id
        except Exception:
            return None

    async def _get_payment_method_id(self, page) -> int | None:
        """Fetch the first payment method ID on file."""
        try:
            result = await page.evaluate("""async (token) => {
                const resp = await fetch('https://hibid.com/graphql', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + token
                    },
                    body: JSON.stringify({
                        query: `query BuyerPayInfo {
                            buyerPayInfo {
                                ... on BuyerPayInfo { id }
                            }
                        }`
                    })
                });
                return await resp.json();
            }""", self._auth_token)
            pay_list = result.get("data", {}).get("buyerPayInfo", [])
            if pay_list:
                return pay_list[0]["id"]
            return None
        except Exception:
            return None

    async def _ensure_registered(self, page) -> bool:
        """Auto-register as bidder for the auction if not already registered."""
        if not hasattr(self, '_auction_id') or not self._auction_id:
            return False
        try:
            pay_id = await self._get_payment_method_id(page)
            if pay_id:
                logger.info(f"Using payment method ID: {pay_id}")

            result = await page.evaluate("""async (args) => {
                const [token, auctionId, payInfoId] = args;
                const resp = await fetch('https://hibid.com/graphql', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + token
                    },
                    body: JSON.stringify({
                        operationName: 'RegisterBuyer',
                        variables: {
                            acceptTermsAndConditions: true,
                            auctionId: auctionId,
                            buyerPayInfoId: payInfoId,
                            notes: null,
                            isShippingRequested: false
                        },
                        query: `mutation RegisterBuyer($acceptTermsAndConditions: Boolean!, $auctionId: Int!, $buyerPayInfoId: Int, $notes: String, $isShippingRequested: Boolean!) {
                            registerBuyer(
                                input: {acceptTermsAndConditions: $acceptTermsAndConditions, auctionId: $auctionId, buyerPayInfoId: $buyerPayInfoId, notes: $notes, isShippingRequested: $isShippingRequested}
                            ) {
                                __typename
                                ... on BuyerRegistrationType { body subject }
                                ... on InvalidInputError { messages }
                            }
                        }`
                    })
                });
                return await resp.json();
            }""", [self._auth_token, self._auction_id, pay_id])

            reg = result.get("data", {}).get("registerBuyer", {})
            if reg.get("__typename") == "BuyerRegistrationType":
                logger.info("Auto-registered for auction")
                return True
            else:
                msg = reg.get("messages", ["Unknown"])
                logger.warning(f"Auto-registration result: {msg}")
                return False
        except Exception as e:
            logger.warning(f"Auto-registration failed: {e}")
            return False

    def cancel(self):
        """Cancel the snipe job."""
        self.cancelled = True
        self.status = "cancelled"
