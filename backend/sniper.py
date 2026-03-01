import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from backend.hibid_scraper import get_browser, parse_lot_id_from_url, LotDetails
from backend.calculator import calculate_true_cost
from backend.discord_notify import (
    send_notification, format_snipe_won, format_snipe_lost, format_snipe_capped,
)
from backend.hibid_auth import login_if_needed

logger = logging.getLogger(__name__)

SNIPE_WINDOW_SECONDS = 3
POLL_INTERVAL = 5
SOFT_CLOSE_RECHECK = 2


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


WAKE_BEFORE_SECONDS = 300  # Wake up 5 minutes before auction end


class SnipeJob:
    def __init__(self, lot_url: str, max_cap: float, premium_pct: float, snipe_id: int | None = None,
                 end_time: datetime | None = None, get_budget=None, log_bid=None):
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

        sleep_until = end - timedelta(seconds=WAKE_BEFORE_SECONDS)
        remaining = (sleep_until - now).total_seconds()

        if remaining <= 0:
            logger.info(f"Snipe {self.snipe_id}: End time is within {WAKE_BEFORE_SECONDS}s, skipping sleep")
            return

        logger.info(
            f"Snipe {self.snipe_id}: Sleeping for {remaining:.0f}s "
            f"(waking at {sleep_until.isoformat()}, auction ends {end.isoformat()})"
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
                self._auction_id = await self._get_auction_id(page)
                if self._auction_id:
                    logger.info(f"Auction ID: {self._auction_id}")
                    await self._ensure_registered(page)
            else:
                logger.warning("No auth token - bid placement may fail")

            while not self.cancelled:
                state = await self._get_auction_state(page)
                if state is None:
                    logger.error("Could not read auction state")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                current_price = state["current_price"]
                increment = state["increment"]
                seconds_left = state["seconds_left"]
                is_ended = state["is_ended"]
                self.last_known_price = current_price
                self.increment = increment

                # Update DB with current price on every poll
                if on_status_change:
                    await on_status_change(self)

                if is_ended:
                    if state.get("we_are_winning"):
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
                    self.status = "capped_out"
                    await send_notification(format_snipe_capped(
                        lot_title=state.get("title", "Unknown"),
                        lot_url=self.lot_url,
                        current_price=current_price,
                        your_cap=self.max_cap,
                    ))
                    break

                # If time parse failed, wait and retry — don't accidentally bid
                if seconds_left < 0:
                    logger.warning(f"Snipe {self.snipe_id}: Could not parse time from '{state.get('time_left', '')}', waiting...")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                if seconds_left > SNIPE_WINDOW_SECONDS:
                    wait_time = min(seconds_left - SNIPE_WINDOW_SECONDS, POLL_INTERVAL)
                    await asyncio.sleep(wait_time)
                    continue

                # Don't bid if we're already winning — avoid bidding against ourselves
                if state.get("we_are_winning"):
                    logger.info(f"Snipe {self.snipe_id}: Already winning at ${current_price}, waiting...")
                    await asyncio.sleep(SOFT_CLOSE_RECHECK)
                    continue

                # Within snipe window - place the bid
                self.status = "bidding"
                if on_status_change:
                    await on_status_change(self)

                bid_amount = next_bid_amount(current_price, increment)
                logger.info(f"Sniping {self.lot_url} at ${bid_amount}")
                success = await self._place_bid(page, bid_amount)

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

            if not state or not state.get("current_price"):
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
                              or "sold" in time_text.lower())
            is_ended = ended_keywords or (seconds_left == 0)

            # Clean title: "Lot # : 1 - Something" -> "Something"
            raw_title = state.get("title") or "Unknown"
            title = re.sub(r"^Lot\s*#\s*:\s*\d+\s*-\s*", "", raw_title).strip()

            # Detect if we're the winning bidder
            # When logged in, HiBid shows "You are the high bidder" or similar
            we_are_winning = False
            try:
                winning_indicator = await page.evaluate("""() => {
                    const body = document.body.innerText;
                    return body.includes('high bidder') ||
                           body.includes('winning') ||
                           body.includes('You are the');
                }""") if not is_ended else False
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

    async def _place_bid(self, page, bid_amount: float = None) -> bool:
        """Place a bid via HiBid's GraphQL API."""
        if not hasattr(self, '_auth_token') or not self._auth_token:
            logger.error("No auth token available for bidding")
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

        # Never exceed this snipe's explicit cap.
        if bid_amount > self.max_cap:
            logger.warning(
                f"[CAP BLOCKED] Snipe {self.snipe_id}: bid ${bid_amount:.2f} exceeds cap ${self.max_cap:.2f}"
            )
            if self.log_bid:
                self.log_bid(self.snipe_id, bid_amount, "cap_blocked", f"max_cap=${self.max_cap:.2f}")
            return False

        # --- GLOBAL BUDGET GATE ---
        if self.get_budget:
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

            current_commitment = self.last_bid_placed if self.last_bid_placed is not None else self.last_known_price
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

        try:
            result = await page.evaluate("""async (args) => {
                const [token, lotId, bidAmount] = args;
                const resp = await fetch('https://hibid.com/graphql', {
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
                return await resp.json();
            }""", [self._auth_token, lot_id, bid_amount])

            bid_data = result.get("data", {}).get("bid", {})
            typename = bid_data.get("__typename", "")

            if typename == "BidResultType":
                status = bid_data.get("bidStatus", "")
                message = bid_data.get("bidMessage", "")
                logger.info(f"Bid result: status={status}, message={message}")

                if status == "WINNING":
                    self.last_bid_placed = bid_amount
                    if self.log_bid:
                        self.log_bid(self.snipe_id, bid_amount, "placed", "WINNING")
                    return True
                elif status == "OUTBID":
                    self.last_bid_placed = bid_amount
                    if self.log_bid:
                        self.log_bid(self.snipe_id, bid_amount, "placed", f"OUTBID suggested=${bid_data.get('suggestedBid', 0)}")
                    logger.info(f"Outbid - suggested: ${bid_data.get('suggestedBid', 0)}")
                    return True  # Bid was placed, just outbid
                elif status == "NO_BID" and message == "RegisterFirst":
                    logger.error("Not registered for this auction")
                    if self.log_bid:
                        self.log_bid(self.snipe_id, bid_amount, "error", "Not registered")
                    return False
                else:
                    logger.warning(f"Unexpected bid status: {status} - {message}")
                    return status not in ("NO_BID",)
            elif typename == "InvalidInputError":
                messages = bid_data.get("messages", [])
                logger.error(f"Bid rejected: {messages}")
                if self.log_bid:
                    self.log_bid(self.snipe_id, bid_amount, "error", f"Rejected: {messages}")
                return False
            else:
                logger.error(f"Unexpected response: {result}")
                return False

        except Exception as e:
            logger.error(f"Error placing GraphQL bid: {e}")
            if self.log_bid:
                self.log_bid(self.snipe_id, bid_amount or 0, "error", str(e))
            return False

    async def _get_auth_token(self, page) -> str | None:
        """Extract JWT auth token from sessionId cookie."""
        try:
            token = await page.evaluate("""() => {
                const cookies = document.cookie.split(';').map(c => c.trim());
                const session = cookies.find(c => c.startsWith('sessionId='));
                return session ? session.split('=').slice(1).join('=') : null;
            }""")
            return token
        except Exception:
            return None

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
