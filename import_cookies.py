"""
Cookie importer & validator — bypasses twikit's broken login flow.

USAGE:
  1. Log into https://x.com in your browser
  2. Use Cookie-Editor (or similar) extension to export cookies as JSON
  3. Save the exported JSON as 'browser_cookies.json' in this project folder
  4. Run: python import_cookies.py
  5. If valid, this creates 'cookies.json' in the twikit-compatible format
  6. Test with: DRY_RUN=1 python bot.py

This script:
  - Reads browser-exported cookies (any common format)
  - Filters to only x.com / twitter.com cookies
  - Strips extra fields twikit doesn't need
  - Validates that critical auth cookies are present
  - Saves in twikit-compatible format
"""

import json
import sys
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).parent
BROWSER_COOKIES_FILE = PROJECT_ROOT / "browser_cookies.json"
COOKIES_FILE = PROJECT_ROOT / "cookies.json"

# Critical cookies X needs for authenticated scraping
CRITICAL_COOKIES = ["auth_token", "ct0"]
# All auth-related cookies we want to keep
RELEVANT_COOKIE_NAMES = {
    "auth_token", "ct0", "twid", "kdt", "auth_multi",
    "att", "guest_id", "personalization_id", "gt", "phx",
    "rweb_optin", "rweb_welcome", "rweb_authorization",
    "lang", "cd_user_id", "_twitter_sess",
}

# Fields twikit cares about
KEEP_FIELDS = {"name", "value", "domain", "path", "expires", "secure", "httpOnly"}


def load_browser_cookies() -> list:
    """Load cookies from browser_cookies.json. Handles various export formats."""
    if not BROWSER_COOKIES_FILE.exists():
        print(f"ERROR: {BROWSER_COOKIES_FILE} not found.")
        print("Export cookies from your browser's Cookie-Editor extension,")
        print("then save them as 'browser_cookies.json' in this folder.")
        return []

    try:
        with open(BROWSER_COOKIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse JSON: {e}")
        return []

    # Handle different export formats
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Cookie-Editor sometimes wraps in {"cookies": [...]}
        if "cookies" in data:
            return data["cookies"]
        # Or might be a single cookie dict
        if "name" in data and "value" in data:
            return [data]
    return []


def filter_and_clean_cookies(cookies: list) -> list:
    """Keep only x.com/twitter.com cookies, strip extra fields."""
    cleaned = []
    seen_names = set()

    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if "name" not in cookie or "value" not in cookie:
            continue

        name = cookie["name"]
        domain = cookie.get("domain", "")

        # Only keep X/Twitter cookies
        if not any(d in domain for d in [".x.com", "x.com", ".twitter.com", "twitter.com"]):
            continue

        # Skip duplicates (keep first occurrence)
        if name in seen_names:
            continue

        # Build clean cookie dict
        clean = {k: v for k, v in cookie.items() if k in KEEP_FIELDS}
        # Ensure required fields
        clean.setdefault("domain", ".x.com")
        clean.setdefault("path", "/")
        clean.setdefault("secure", True)
        clean.setdefault("httpOnly", False)

        cleaned.append(clean)
        seen_names.add(name)

    return cleaned


def validate_cookies(cookies: list) -> tuple[bool, list]:
    """Check that critical auth cookies are present. Returns (is_valid, missing)."""
    names = {c["name"] for c in cookies}
    missing = [c for c in CRITICAL_COOKIES if c not in names]
    return (len(missing) == 0, missing)


def save_twikit_cookies(cookies: list) -> None:
    """Save cookies in twikit-compatible format (plain JSON list)."""
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)
    print(f"Saved {len(cookies)} cookies to {COOKIES_FILE}")


def main():
    print("=" * 60)
    print("twikit Cookie Importer")
    print("=" * 60)

    # Step 1: Load
    cookies = load_browser_cookies()
    if not cookies:
        print("\nNo cookies found. Follow the steps in the README:")
        print("  1. Log into https://x.com in your browser")
        print("  2. Use Cookie-Editor extension to export cookies as JSON")
        print("  3. Save as 'browser_cookies.json' in this folder")
        print("  4. Re-run this script")
        return 1

    print(f"\nLoaded {len(cookies)} cookies from browser_cookies.json")

    # Step 2: Filter & clean
    cleaned = filter_and_clean_cookies(cookies)
    print(f"Filtered to {len(cleaned)} x.com/twitter.com cookies")

    if not cleaned:
        print("\nERROR: No x.com or twitter.com cookies found in the export.")
        print("Make sure you exported cookies WHILE on https://x.com (not another site).")
        return 1

    # Step 3: Validate
    is_valid, missing = validate_cookies(cleaned)
    if not is_valid:
        print(f"\nERROR: Missing critical cookies: {missing}")
        print("These cookies are required for twikit to authenticate.")
        print("\nLikely causes:")
        print("  - You're not actually logged into X in the browser")
        print("  - You exported cookies for the wrong domain")
        print("  - Your X session expired")
        print("\nFix: Log into https://x.com, refresh the page, then re-export.")
        return 1

    print(f"All critical cookies present: {CRITICAL_COOKIES}")

    # Step 4: Show summary
    print("\nCookie summary:")
    for c in cleaned:
        name = c["name"]
        value_preview = c["value"][:8] + "..." if len(c["value"]) > 12 else c["value"]
        print(f"  {name:25s} = {value_preview}")

    # Step 5: Save
    save_twikit_cookies(cleaned)

    print("\n" + "=" * 60)
    print("SUCCESS! cookies.json is ready for twikit.")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Test with dry run:")
    print("     $env:DRY_RUN='1'; python bot.py   (PowerShell)")
    print("     DRY_RUN=1 python bot.py            (Linux/Mac)")
    print("  2. If dry run works, post for real:")
    print("     python bot.py")
    print("\nFor GitHub Actions:")
    print("  1. Open cookies.json, copy ALL its contents")
    print("  2. Add a GitHub Secret named TWIKIT_COOKIES with that content")
    return 0


if __name__ == "__main__":
    sys.exit(main())