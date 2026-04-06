from backend.sniper import (
    ESTIMATED_WAKE_BEFORE_SECONDS,
    MAX_REASONABLE_PAGE_SECONDS,
    REBID_COOLDOWN_SECONDS,
    SnipeJob,
    TERMINAL_SNIPE_STATUSES,
    WAKE_BEFORE_SECONDS,
    next_bid_amount,
    projected_exposure_total,
    should_bid,
)
from datetime import datetime, timedelta, timezone
import asyncio

def test_should_bid_under_cap():
    assert should_bid(current_price=50.0, max_cap=100.0, increment=5.0) is True

def test_should_not_bid_at_cap():
    assert should_bid(current_price=100.0, max_cap=100.0, increment=5.0) is False

def test_should_not_bid_over_cap():
    assert should_bid(current_price=95.0, max_cap=100.0, increment=10.0) is False

def test_should_bid_exact_cap():
    assert should_bid(current_price=95.0, max_cap=100.0, increment=5.0) is True

def test_next_bid_amount():
    assert next_bid_amount(current_price=50.0, increment=5.0) == 55.0

def test_next_bid_amount_large_increment():
    assert next_bid_amount(current_price=200.0, increment=10.0) == 210.0


def test_projected_exposure_total_replaces_existing_commitment():
    # exposure_total includes this snipe at $40; replacing with $55 should add $15
    assert projected_exposure_total(120.0, 40.0, 55.0) == 135.0


def test_projected_exposure_total_with_no_prior_commitment():
    assert projected_exposure_total(120.0, 0.0, 25.0) == 145.0


def test_mark_auth_failed_sets_terminal_status_and_logs():
    calls = []

    def log_bid(snipe_id, bid_amount, result, message=""):
        calls.append((snipe_id, bid_amount, result, message))

    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        log_bid=log_bid,
    )

    job._mark_auth_failed(22.0, "missing session")

    assert job.status == "auth_failed"
    assert "auth_failed" in TERMINAL_SNIPE_STATUSES
    assert "error" in TERMINAL_SNIPE_STATUSES
    assert calls == [(123, 22.0, "auth_failed", "missing session")]


class _FakePage:
    def __init__(self, responses):
        self.responses = list(responses)

    async def evaluate(self, _script, _args=None):
        if not self.responses:
            raise AssertionError("No more fake responses configured")
        return self.responses.pop(0)

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, *_args, **_kwargs):
        return None


def test_choose_live_end_time_prefers_earlier_when_reads_disagree():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
    )
    now = datetime.now(timezone.utc)
    chosen, reliable = job._choose_live_end_time(now + timedelta(seconds=500), now + timedelta(seconds=150))

    assert reliable is False
    assert chosen == now + timedelta(seconds=150)


def test_compute_seconds_left_uses_more_urgent_source_and_ignores_absurd_page_timer():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        end_time=datetime.now(timezone.utc) + timedelta(seconds=45),
    )

    seconds_left = job._compute_seconds_left(MAX_REASONABLE_PAGE_SECONDS + 1)

    assert 0 < seconds_left <= 45


def test_estimated_end_times_wake_earlier():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        end_time_estimated=True,
    )

    assert job._wake_before_seconds() == ESTIMATED_WAKE_BEFORE_SECONDS


def test_reliable_end_times_use_normal_wake_buffer():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        end_time_estimated=True,
    )
    job.end_time_reliable = True

    assert job._wake_before_seconds() == WAKE_BEFORE_SECONDS


def test_place_bid_treats_accepted_as_success_and_records_bid():
    calls = []

    def log_bid(snipe_id, bid_amount, result, message=""):
        calls.append((snipe_id, bid_amount, result, message))

    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        log_bid=log_bid,
    )
    job._auth_token = "token"

    page = _FakePage([
        {
            "data": {
                "bid": {
                    "__typename": "BidResultType",
                    "bidStatus": "ACCEPTED",
                    "suggestedBid": 0,
                    "bidMessage": None,
                }
            }
        }
    ])

    result = asyncio.run(job._place_bid(page, 25.0))

    assert result is True
    assert job.last_bid_placed == 25.0
    assert calls == [(123, 25.0, "placed", "ACCEPTED")]


def test_place_bid_retries_with_suggested_bid_on_increase_bid():
    calls = []

    def log_bid(snipe_id, bid_amount, result, message=""):
        calls.append((snipe_id, bid_amount, result, message))

    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        log_bid=log_bid,
    )
    job._auth_token = "token"

    page = _FakePage([
        {
            "data": {
                "bid": {
                    "__typename": "BidResultType",
                    "bidStatus": "NO_BID",
                    "suggestedBid": 28,
                    "bidMessage": "IncreaseBid",
                }
            }
        },
        {
            "data": {
                "bid": {
                    "__typename": "BidResultType",
                    "bidStatus": "WINNING",
                    "suggestedBid": 0,
                    "bidMessage": None,
                }
            }
        },
    ])

    result = asyncio.run(job._place_bid(page, 25.0))

    assert result is True
    assert job.last_bid_placed == 28.0
    assert calls == [(123, 28.0, "placed", "WINNING")]


def test_place_bid_treats_previous_max_bid_as_success():
    calls = []

    def log_bid(snipe_id, bid_amount, result, message=""):
        calls.append((snipe_id, bid_amount, result, message))

    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        log_bid=log_bid,
    )
    job._auth_token = "token"

    page = _FakePage([
        {
            "data": {
                "bid": {
                    "__typename": "BidResultType",
                    "bidStatus": "NO_BID",
                    "suggestedBid": 25,
                    "bidMessage": "PreviousMaxBid",
                }
            }
        }
    ])

    result = asyncio.run(job._place_bid(page, 25.0))

    assert result is True
    assert job.last_bid_placed == 25.0
    assert calls == [(123, 25.0, "placed", "ACCEPTED PreviousMaxBid")]


def test_place_bid_treats_previous_max_bid_with_whitespace_as_success():
    calls = []

    def log_bid(snipe_id, bid_amount, result, message=""):
        calls.append((snipe_id, bid_amount, result, message))

    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
        log_bid=log_bid,
    )
    job._auth_token = "token"

    page = _FakePage([
        {
            "data": {
                "bid": {
                    "__typename": "BidResultType",
                    "bidStatus": " NO_BID ",
                    "suggestedBid": 25,
                    "bidMessage": " PreviousMaxBid ",
                }
            }
        }
    ])

    result = asyncio.run(job._place_bid(page, 25.0))

    assert result is True
    assert job.last_bid_placed == 25.0
    assert calls == [(123, 25.0, "placed", "ACCEPTED PreviousMaxBid")]


def test_should_submit_bid_blocks_duplicate_during_cooldown():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
    )
    now = datetime.now(timezone.utc)
    job.last_bid_attempt_at = now - timedelta(seconds=REBID_COOLDOWN_SECONDS - 1)
    job.last_bid_attempt_amount = 25.0
    job.last_bid_observed_price = 20.0

    allowed = job._should_submit_bid(25.0, 20.0, 120.0)

    assert allowed is False


def test_should_submit_bid_blocks_when_current_price_is_still_within_our_committed_max():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
    )
    job.last_bid_placed = 25.0

    allowed = job._should_submit_bid(26.0, 25.0, 20.0)

    assert allowed is False


def test_should_submit_bid_allows_retry_when_price_changed():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
    )
    now = datetime.now(timezone.utc)
    job.last_bid_attempt_at = now - timedelta(seconds=1)
    job.last_bid_attempt_amount = 25.0
    job.last_bid_observed_price = 20.0

    allowed = job._should_submit_bid(26.0, 21.0, 115.0)

    assert allowed is True


def test_should_submit_bid_allows_retry_after_outbid_above_committed_max():
    job = SnipeJob(
        lot_url="https://hibid.com/lot/123",
        max_cap=50.0,
        premium_pct=15.0,
        snipe_id=123,
    )
    job.last_bid_placed = 25.0

    allowed = job._should_submit_bid(27.0, 26.0, 18.0)

    assert allowed is True
