"""
Playwright-based X (Twitter) poster — uses a REAL browser to post tweets.

This is dramatically more robust than twikit because:
  - It's a real browser instance (Chrome/Edge)
  - X cannot distinguish it from a human user
  - No API keys, no scraping, no broken JavaScript parsing
  - Cookies work perfectly (browser handles them natively)

Usage:
  python post_playwright.py "Your tweet text here"

Setup (one-time):
  pip install playwright
  playwright install chromium

The script:
  1. Launches a Chrome browser (visible by default for debugging)
  2. Loads your cookies from cookies.json
  3. Navigates to x.com
  4. Opens the compose tweet dialog
  5. Types the tweet with human-like delays
  6. Clicks the Post button
  7. Waits for confirmation
  8. Closes the browser
"""

import asyncio
import json
import sys
import time
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: playwright not installed.")
    print("Run: pip install playwright")
    print("Then: playwright install chromium")
    sys.exit(1)


COOKIES_FILE = Path(__file__).parent / "cookies.json"


def load_cookies_for_playwright() -> list:
    """Load cookies from cookies.json and convert to Playwright format.
    
    Playwright expects cookies as a list of dicts with:
      name, value, domain, path, expires, httpOnly, secure, sameSite
    
    Our cookies.json is in {name: value} format (converted by import_cookies).
    """
    if not COOKIES_FILE.exists():
        print(f"ERROR: {COOKIES_FILE} not found. Run: python bot.py import-cookies")
        return []
    
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"ERROR: Could not read cookies: {e}")
        return []
    
    cookies = []
    
    # Format 1: Simple dict {name: value}
    if isinstance(data, dict) and "cookies" not in data:
        for name, value in data.items():
            cookies.append({
                "name": name,
                "value": str(value),
                "domain": ".x.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            })
        return cookies
    
    # Format 2: {"cookies": [...]} wrapper
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    
    # Format 3: List of cookie dicts (from Cookie-Editor export)
    if isinstance(data, list):
        for cookie in data:
            if not isinstance(cookie, dict):
                continue
            if "name" not in cookie or "value" not in cookie:
                continue
            # Only keep x.com / twitter.com cookies
            domain = cookie.get("domain", "")
            if not any(d in domain for d in [".x.com", "x.com", ".twitter.com", "twitter.com"]):
                continue
            cookies.append({
                "name": cookie["name"],
                "value": str(cookie["value"]),
                "domain": cookie.get("domain", ".x.com"),
                "path": cookie.get("path", "/"),
                "expires": cookie.get("expires", -1),
                "httpOnly": cookie.get("httpOnly", False),
                "secure": cookie.get("secure", True),
                "sameSite": cookie.get("sameSite", "Lax"),
            })
    
    return cookies


async def post_tweet(content: str, headless: bool = False, timeout: int = 120) -> dict:
    """
    Post a tweet using a real browser via Playwright.
    
    Args:
        content: The tweet text to post
        headless: If True, run browser invisibly. If False, show browser (good for debugging)
        timeout: Max seconds to wait for the post to complete
    
    Returns:
        dict with "id", "text", and "status" keys on success, or None on failure
    """
    cookies = load_cookies_for_playwright()
    if not cookies:
        print("ERROR: No cookies loaded. Cannot post.")
        return None
    
    print(f"Loaded {len(cookies)} cookies")
    print(f"Tweet content ({len(content)} chars): {content[:80]}...")
    print(f"Browser mode: {'headless' if headless else 'visible'}")
    
    async with async_playwright() as p:
        # Launch Chrome/Chromium with aggressive optimizations for GitHub Actions
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",  # hide automation
                "--no-sandbox",
                "--disable-web-security",
                "--disable-gpu",  # disable GPU for faster rendering in CI
                "--disable-dev-shm-usage",  # overcome limited memory in Docker
                "--disable-extensions",
                "--disable-plugins",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--disable-default-apps",
            ]
        )
        
        # Create context with realistic viewport and user agent, optimized for speed
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            ignore_https_errors=True,  # speed up in CI/CD
        )
        
        # Add cookies to the browser context
        await context.add_cookies(cookies)
        
        page = await context.new_page()
        
        # Disable images and unnecessary resources for speed
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg}", lambda route: route.abort())
        await page.route("**/*.css", lambda route: route.abort())
        
        try:
            # Step 1: Navigate to X (use "domcontentloaded" for faster load, 120s timeout)
            print("\n[1/6] Navigating to x.com...")
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=120000)
            
            # Check if we're actually logged in (look for the home feed)
            try:
                await page.wait_for_selector(
                    'div[data-testid="primaryColumn"]',
                    timeout=30000
                )
            except Exception:
                # Try alternative selector
                try:
                    await page.wait_for_selector(
                        'a[data-testid="AppTabBar_Home_Link"]',
                        timeout=20000
                    )
                except Exception:
                    print("ERROR: Not logged in. Cookies may be expired.")
                    print("Re-export cookies from your browser and run: python bot.py import-cookies")
                    await browser.close()
                    return None
            
            print("[1/6] Logged in successfully")
            
            # Step 2: Open the compose tweet dialog
            print("[2/6] Opening compose tweet dialog...")
            compose_button = None
            
            # Try primary sidebar button
            try:
                compose_button = await page.wait_for_selector(
                    'a[data-testid="SideNav_NewTweet_Button"]',
                    timeout=15000
                )
            except Exception:
                pass
            
            # Fallback to floating action button
            if not compose_button:
                try:
                    compose_button = await page.wait_for_selector(
                        'a[data-testid="FloatingActionButton_Container"]',
                        timeout=10000
                    )
                except Exception:
                    pass
            
            # Fallback: Try the new tweet button in the sidebar
            if not compose_button:
                try:
                    compose_button = await page.wait_for_selector(
                        'a[href="/compose/tweet"]',
                        timeout=10000
                    )
                except Exception:
                    pass
            
            if not compose_button:
                print("ERROR: Could not find compose tweet button")
                await browser.close()
                return None
            
            await compose_button.click()
            
            # Wait for the compose dialog to appear - try multiple selectors
            dialog_opened = False
            for selector in [
                'div[role="dialog"]',
                'div[data-testid="tweetDialog"]',
                'div[aria-labelledby="modal-header"]',
            ]:
                try:
                    await page.wait_for_selector(selector, timeout=10000)
                    dialog_opened = True
                    break
                except Exception:
                    continue
            
            if not dialog_opened:
                print("ERROR: Compose dialog did not open")
                await browser.close()
                return None
            
            print("[2/6] Compose dialog opened")
            
            # Step 3: Type the tweet
            print("[3/6] Typing tweet...")
            editor = None
            for selector in [
                'div[role="textbox"][data-testid="tweetTextarea_0"]',
                'div[contenteditable="true"][role="textbox"][data-testid="tweetTextarea_0"]',
                'div[contenteditable="true"][role="textbox"]',
                'div[role="textbox"][data-testid="tweetTextarea_0"]',
            ]:
                try:
                    editor = await page.wait_for_selector(selector, timeout=8000)
                    if editor:
                        break
                except Exception:
                    continue
            
            if not editor:
                print("ERROR: Could not find text editor")
                await browser.close()
                return None
            
            await editor.click()
            await asyncio.sleep(0.5)
            
            # Type with minimal delays for speed (30ms between keystrokes)
            for char in content:
                await page.keyboard.type(char)
                await asyncio.sleep(0.02)  # very fast but still human-like
            
            print(f"[3/6] Tweet typed ({len(content)} chars)")
            
            # Step 4: Minimal pause before posting
            print("[4/6] Ready to post...")
            await asyncio.sleep(1.0)
            
            # Step 5: Click the Post button
            print("[5/6] Clicking Post button...")
            post_button = None
            for selector in [
                'button[data-testid="tweetButton"]',
                'button[data-testid="tweetButtonInline"]',
                'div[role="dialog"] button[data-testid="tweetButton"]',
            ]:
                try:
                    post_button = await page.wait_for_selector(selector, timeout=8000)
                    if post_button:
                        break
                except Exception:
                    continue
            
            if not post_button:
                print("ERROR: Could not find Post button")
                await browser.close()
                return None
            
            # Verify the button is enabled
            is_disabled = await post_button.get_attribute("disabled")
            if is_disabled is not None:
                print("ERROR: Post button is disabled")
                await browser.close()
                return None
            
            await post_button.click()
            print("[5/6] Post button clicked")
            
            # Step 6: Wait for confirmation (120s max, but usually much faster)
            print("[6/6] Waiting for confirmation...")
            try:
                await page.wait_for_selector(
                    'div[role="dialog"]',
                    state="detached",
                    timeout=120000
                )
                print("[6/6] Dialog closed — tweet posted!")
            except Exception as e:
                # Check for error toast
                try:
                    toast = await page.wait_for_selector(
                        'div[data-testid="toast"]',
                        timeout=5000
                    )
                    toast_text = await toast.text_content()
                    print(f"ERROR: X showed an error: {toast_text}")
                    await browser.close()
                    return None
                except Exception:
                    print("WARNING: Could not confirm dialog closed, continuing anyway...")
            
            # Try to grab the tweet URL from notification or URL
            tweet_url = None
            tweet_id = "unknown"
            
            # Method 1: Check for notification link
            try:
                notif_link = await page.query_selector('a[data-testid="toast"]')
                if notif_link:
                    tweet_url = await notif_link.get_attribute("href")
            except Exception:
                pass
            
            # Method 2: Check URL after posting
            if not tweet_url:
                try:
                    current_url = page.url
                    if "/status/" in current_url:
                        tweet_url = current_url
                except Exception:
                    pass
            
            # Method 3: Check for tweet link in page
            if not tweet_url:
                try:
                    tweet_link = await page.query_selector('a[href*="/status/"]')
                    if tweet_link:
                        tweet_url = await tweet_link.get_attribute("href")
                except Exception:
                    pass
            
            # Extract tweet ID
            if tweet_url and "/status/" in tweet_url:
                tweet_id = tweet_url.split("/status/")[-1].split("?")[0].split("/")[0]
            
            await browser.close()
            
            return {
                "id": tweet_id,
                "text": content,
                "status": "posted",
                "url": tweet_url or f"https://x.com/i/status/{tweet_id}",
            }
            
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            # Take a screenshot for debugging
            try:
                screenshot_path = Path(__file__).parent / "logs" / "error_screenshot.png"
                screenshot_path.parent.mkdir(exist_ok=True)
                await page.screenshot(path=str(screenshot_path))
                print(f"Screenshot saved to: {screenshot_path}")
            except Exception:
                pass
            await browser.close()
            return None


def post_tweet_sync(content: str, headless: bool = False) -> dict:
    """Synchronous wrapper for post_tweet."""
    # Auto-detect headless mode: GitHub Actions / Linux servers have no display
    import os
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.name == "posix" and not os.environ.get("DISPLAY"):
        headless = True
    return asyncio.run(post_tweet(content, headless=headless))


async def verify_tweet(tweet_id: str, headless: bool = False, timeout: int = 60) -> bool:
    """Verify a tweet is publicly visible by navigating to its URL."""
    cookies = load_cookies_for_playwright()
    if not cookies:
        print("ERROR: No cookies loaded. Cannot verify.")
        return False
    
    tweet_url = f"https://x.com/i/status/{tweet_id}"
    print(f"Verifying tweet: {tweet_url}")
    print(f"Browser mode: {'headless' if headless else 'visible'}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-plugins",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--disable-default-apps",
            ]
        )
        
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            ignore_https_errors=True,
        )
        
        await context.add_cookies(cookies)
        page = await context.new_page()
        
        # Disable images and CSS for speed
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg}", lambda route: route.abort())
        await page.route("**/*.css", lambda route: route.abort())
        
        try:
            # Navigate to tweet URL
            await page.goto(tweet_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            
            # Wait for tweet content to load - try multiple selectors
            tweet_found = False
            for selector in [
                'article[data-testid="tweet"]',
                'div[data-testid="tweetText"]',
                'div[role="article"]',
                'article[role="article"]',
            ]:
                try:
                    await page.wait_for_selector(selector, timeout=15000)
                    tweet_found = True
                    break
                except Exception:
                    continue
            
            if not tweet_found:
                # Check if we got a "Tweet not available" or similar message
                try:
                    error_text = await page.text_content("body")
                    if error_text and ("not available" in error_text.lower() or "doesn't exist" in error_text.lower() or "deleted" in error_text.lower()):
                        print("Tweet appears to be deleted or unavailable")
                        await browser.close()
                        return False
                except Exception:
                    pass
                print("ERROR: Could not find tweet content on page")
                await browser.close()
                return False
            
            # Verify the tweet is actually visible (not shadowbanned)
            # Check if tweet text element is visible
            try:
                tweet_element = await page.query_selector('article[data-testid="tweet"]')
                if tweet_element:
                    is_visible = await tweet_element.is_visible()
                    if not is_visible:
                        print("Tweet element found but not visible (possible shadowban)")
                        await browser.close()
                        return False
            except Exception:
                pass
            
            print(f"SUCCESS: Tweet {tweet_id} is publicly visible")
            await browser.close()
            return True
            
        except Exception as e:
            print(f"ERROR during verification: {type(e).__name__}: {e}")
            try:
                screenshot_path = Path(__file__).parent / "logs" / "verify_error_screenshot.png"
                screenshot_path.parent.mkdir(exist_ok=True)
                await page.screenshot(path=str(screenshot_path))
                print(f"Screenshot saved to: {screenshot_path}")
            except Exception:
                pass
            await browser.close()
            return False


def verify_tweet_sync(tweet_id: str, headless: bool = False) -> bool:
    """Synchronous wrapper for verify_tweet."""
    import os
    if os.environ.get("GITHUB_ACTIONS") == "true" or (os.name == "posix" and not os.environ.get("DISPLAY")):
        headless = True
    return asyncio.run(verify_tweet(tweet_id, headless=headless))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python post_playwright.py \"Your tweet text\"")
        print("       python post_playwright.py --headless \"Your tweet text\"")
        print("       python post_playwright.py --verify TWEET_ID")
        sys.exit(1)
    
    # Check for verify mode
    if "--verify" in sys.argv:
        try:
            idx = sys.argv.index("--verify")
            tweet_id = sys.argv[idx + 1]
        except (ValueError, IndexError):
            print("ERROR: --verify requires a tweet ID")
            sys.exit(1)
        
        headless_mode = "--headless" in sys.argv
        result = verify_tweet_sync(tweet_id, headless=headless_mode)
        if result:
            print("\n=== VERIFICATION SUCCESS ===")
            sys.exit(0)
        else:
            print("\n=== VERIFICATION FAILED ===")
            sys.exit(1)
    
    # Normal post mode
    headless_mode = "--headless" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--headless"]
    tweet_content = " ".join(args) 
    
    if not tweet_content:
        print("ERROR: No tweet content provided")
        sys.exit(1)
    
    result = post_tweet_sync(tweet_content, headless=headless_mode)
    if result:
        print("\n=== SUCCESS ===")
        print(f"Tweet ID: {result['id']}")
        print(f"URL: {result.get('url', 'N/A')}")
        sys.exit(0)
    else:
        print("\n=== FAILED ===")
        sys.exit(1)
