# CRITICAL: Global Spend Cap / Safety Limits

**Priority:** MUST HAVE before any real use
**Reason:** Bot places bids autonomously. Without a hard ceiling, a bug or unexpected behavior could rack up thousands in charges overnight.

## Requirements

### 1. Global Spend Cap (Hard Limit)
- User-configurable dollar amount (e.g., $500)
- **Applies across ALL active snipes combined** - not per-snipe, total exposure
- Bot MUST check this before every single bid placement
- If placing a bid would push total exposure over the cap → refuse to bid, mark snipe as `capped_out`
- Stored in DB (persists across restarts)
- Default: $0 (disabled/must be set before sniping works)

### 2. What Counts Toward the Cap
- Sum of `max_cap` for all snipes with status `watching` or `bidding`
- Once a snipe is `won`, its winning bid amount counts as "spent"
- `lost`, `capped_out`, `cancelled` snipes don't count
- Formula: `total_exposure = sum(active snipe max_caps) + sum(won snipe winning_bids)`

### 3. Per-Snipe Sanity Checks
- Max single snipe cap limit (e.g., no single snipe over $200 unless explicitly overridden)
- Confirmation/warning if a snipe's max_cap is unusually high relative to the global cap

### 4. UI Requirements
- Global spend cap setting prominently displayed (not buried in settings)
- Current exposure shown: "$127 / $500 budget used"
- Color coding: green (<50%), yellow (50-80%), red (>80%)
- Block snipe creation if it would exceed remaining budget

### 5. Kill Switch
- One-click "STOP ALL SNIPES" button that immediately cancels everything
- Should be the most visible/accessible action in the UI

### 6. Supervision Mode (Gradual Trust)
- **Phase 1 - "Confirm Every Bid"**: Bot monitors and when it's time to bid, sends a Discord notification asking for approval. User replies (or clicks in UI) to confirm. Times out = no bid.
- **Phase 2 - "Notify on Bid"**: Bot bids automatically but sends Discord notification for every bid placed, so user can cancel if something looks wrong.
- **Phase 3 - "Autonomous"**: Bot runs fully on its own with just win/loss/capped notifications (current behavior).
- User picks their comfort level per-snipe or globally
- Can always downgrade back to a more supervised mode

### 7. Activity Log / Audit Trail
- Every bid attempt logged with timestamp, lot, amount, result
- Viewable in the UI (new "Activity" tab or section)
- Makes it easy to review what happened overnight
- Discord notifications for all state changes (not just win/loss)

## Implementation Notes

- Check must happen in `sniper.py` `_place_bid()` right before the GraphQL call
- Also check in `POST /api/snipes` and `POST /api/snipes/from-browser` when creating
- Race condition consideration: two snipes bidding simultaneously - use a lock or atomic check
- Log every bid attempt for audit trail
