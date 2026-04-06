#!/usr/bin/env python3.11
"""Auto-refresh HiBid session cookies using undetected-chromedriver.

Bypasses Cloudflare Turnstile by using a real Chrome instance.
Requires: pip install undetected-chromedriver (python3.11)
Requires: Xvfb running on :99
"""
import json
import os
import random
import sys
import time

COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "hibid_cookies.json")
HIBID_USER = os.environ.get("HIBID_EMAIL", "blender7")
HIBID_PASS = os.environ.get("HIBID_PASSWORD", "hkF#8Mz4LhG7Uu9")


def check_needs_refresh() -> bool:
    """Return True if cookies are expired or expire within 24 hours."""
    if not os.path.exists(COOKIE_FILE):
        return True
    try:
        with open(COOKIE_FILE) as f:
            cookies = json.load(f)
        session = next((c for c in cookies if c["name"] == "sessionId"), None)
        if not session or not session.get("expirationDate"):
            return True
        # Refresh if expiring within 24 hours
        return time.time() > (session["expirationDate"] - 86400)
    except Exception:
        return True


def refresh_cookies() -> bool:
    """Login to HiBid and save fresh cookies. Returns True on success."""
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By

    os.environ["DISPLAY"] = ":99"

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,720")

    driver = uc.Chrome(options=options, headless=False)

    try:
        driver.get("https://hibid.com/pf/login?referral=/&action=generic")
        time.sleep(6)

        # Dismiss cookie consent
        for b in driver.find_elements(By.TAG_NAME, "button"):
            try:
                if "disagree" in b.text.lower():
                    b.click()
                    time.sleep(2)
                    break
            except:
                continue

        # Fill username
        username = driver.find_element(By.ID, "username-input")
        username.click()
        time.sleep(0.3)
        for char in HIBID_USER:
            username.send_keys(char)
            time.sleep(random.uniform(0.05, 0.15))

        # Wait for Turnstile + click Continue
        for i in range(30):
            time.sleep(2)
            try:
                for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
                    if btn.text.strip().lower() in ("continue",) and btn.is_enabled():
                        btn.click()
                        time.sleep(3)
                        break
                password = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                if password.is_displayed():
                    break
            except:
                pass
        else:
            print("ERROR: Turnstile did not solve within 60s")
            return False

        # Fill password
        password = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        for char in HIBID_PASS:
            password.send_keys(char)
            time.sleep(random.uniform(0.05, 0.15))
        time.sleep(0.5)

        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        time.sleep(8)

        all_cookies = driver.get_cookies()
        session = [c for c in all_cookies if c["name"] == "sessionId"]
        if not session:
            print("ERROR: No sessionId after login")
            return False

        # Save cookies
        hibid_cookies = [c for c in all_cookies if "hibid" in c.get("domain", "")]
        saved = []
        for c in hibid_cookies:
            sc = {"name": c["name"], "value": c["value"],
                  "domain": c["domain"], "path": c["path"]}
            if c.get("expiry"):
                sc["expirationDate"] = c["expiry"]
            if c.get("httpOnly"):
                sc["httpOnly"] = True
            if c.get("secure"):
                sc["secure"] = True
            saved.append(sc)

        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w") as f:
            json.dump(saved, f)

        from datetime import datetime
        exp = datetime.fromtimestamp(session[0]["expiry"])
        print(f"OK: Saved {len(saved)} cookies, expires {exp}")
        return True

    except Exception as e:
        print(f"ERROR: {e}")
        return False
    finally:
        driver.quit()


if __name__ == "__main__":
    if not check_needs_refresh():
        print("Cookies still valid, skipping refresh")
        sys.exit(0)

    print("Cookies need refresh, logging in...")
    success = refresh_cookies()
    sys.exit(0 if success else 1)
