import os
import logging
from playwright.async_api import Page

logger = logging.getLogger(__name__)

HIBID_EMAIL = os.environ.get("HIBID_EMAIL", "")
HIBID_PASSWORD = os.environ.get("HIBID_PASSWORD", "")


async def login_if_needed(page: Page) -> bool:
    """Check if logged in via cookies. Returns True if logged in.

    Since HiBid uses Cloudflare Turnstile CAPTCHA, automated login is blocked.
    Instead, we rely on cookies imported from the user's browser via the
    /api/cookies endpoint. The persistent browser profile also preserves
    session cookies between container restarts.
    """
    current_url = page.url
    if "hibid.com" not in current_url:
        await page.goto("https://www.hibid.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # Check for login link - if it exists, we're NOT logged in
    # Real selector: .login-link -> "Sign In"
    login_link = await page.query_selector(".login-link")
    if not login_link:
        # No login link visible = we're logged in
        logger.info("Already logged in to HiBid (no login link found)")
        return True

    # Check login link text to be sure
    login_text = await _safe_text(login_link)
    if login_text and "sign in" in login_text.lower():
        logger.warning(
            "Not logged in to HiBid. Import cookies via Settings tab "
            "or log in manually in the browser profile."
        )
        return False

    # If login link exists but doesn't say "Sign In", might be logged in
    logger.info("Login status unclear, proceeding anyway")
    return True


async def _safe_text(el) -> str | None:
    """Safely get inner text from an element."""
    try:
        return await el.inner_text()
    except Exception:
        return None
