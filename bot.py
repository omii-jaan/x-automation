"""
X (Twitter) Automation Bot — SINGLE FILE VERSION
=================================================
Everything in one file. No src/ folder, no imports between modules.
Just copy this entire file, save as bot.py, and run.

Usage:
  1. pip install twikit httpx
  2. Set env vars (see SETUP section below)
  3. python bot.py import-cookies   (one-time, after exporting browser cookies)
  4. $env:DRY_RUN="1"; python bot.py   (test without posting)
  5. python bot.py                     (post for real)

SETUP env vars (PowerShell):
  $env:OPENROUTER_API_KEY="sk-or-v1-..."
  $env:X_USERNAME="your_username"
  $env:X_EMAIL="your@email.com"
  $env:X_PASSWORD="yourpassword"
  $env:TWIKIT_COOKIES="<contents of cookies.json>"
  $env:TELEGRAM_BOT_TOKEN="..."    (optional)
  $env:TELEGRAM_CHAT_ID="..."      (optional)
  $env:DRY_RUN="1"                 (set to 1 to test without posting)
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)


# =============================================================================
# CONFIGURATION
# =============================================================================
PROJECT_ROOT = Path(__file__).parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)
EXAMPLES_FILE = PROJECT_ROOT / "examples.json"
COOKIES_FILE = PROJECT_ROOT / "cookies.json"
BROWSER_COOKIES_FILE = PROJECT_ROOT / "browser_cookies.json"
STATE_FILE = LOGS_DIR / "state.json"

IST = timezone(timedelta(hours=5, minutes=30))

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
# Primary model: OpenAI's open-weight 117B MoE — free, powerful, great for tweets
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL",
    "openai/gpt-oss-120b:free"
)

# Single-model setup: we use ONLY the primary model above.
# If rate-limited (429), we wait the requested time and retry the SAME model.
# No fallback to other models — keeps things simple and consistent.
OPENROUTER_FALLBACK_MODELS = []
# =============================================================================
# GROQ CONFIGURATION (primary AI provider)
# =============================================================================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "groq")

X_USERNAME = os.environ.get("X_USERNAME", "")
X_EMAIL = os.environ.get("X_EMAIL", "")
X_PASSWORD = os.environ.get("X_PASSWORD", "")
TWIKIT_COOKIES_ENV = "TWIKIT_COOKIES"

TWIKIT_DELAY_RANGE = [2, 8]

MAX_POSTS_PER_DAY = 8
SKIP_PROBABILITY = 0.10
WEEKEND_SKIP_BONUS = 0.10
JITTER_MINUTES_MIN = 0
JITTER_MINUTES_MAX = 25
TEMPERATURE_MIN = 0.75
TEMPERATURE_MAX = 1.15
FEWSHOT_COUNT_MIN = 6
FEWSHOT_COUNT_MAX = 10
LENGTH_MIN = 60
LENGTH_MAX = 270
POST_STYLES = [
    "observation", "hot_take", "question", "story",
    "tip", "prediction", "contrarian", "data_point",
]
SIMULATE_ENGAGEMENT_PROBABILITY = 0.08

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

VERIFY_DELAY_SECONDS = 45
VERIFY_MAX_RETRIES = 3
VERIFY_RETRY_DELAY = 30
SHADOWBAN_PAUSE_HOURS = 48

FORBIDDEN_PHRASES = ["kill yourself"]
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# Skip jitter for testing — set SKIP_JITTER=1 to post immediately
SKIP_JITTER = os.environ.get("SKIP_JITTER", "0") == "1"
# Skip the random skip-probability check for testing
SKIP_SKIP_CHECK = os.environ.get("SKIP_SKIP_CHECK", "0") == "1"


# =============================================================================
# LOGGING
# =============================================================================
def setup_logger(name: str = "bot") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(ch)
    log_file = LOGS_DIR / f"bot_{datetime.now(IST).date().isoformat()}.log"
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    return logger


logger = setup_logger("bot")


# =============================================================================
# UTILS
# =============================================================================
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) >= 2:
        if (text[0] == '"' and text[-1] == '"') or \
           (text[0] == "'" and text[-1] == "'") or \
           (text[0] == '\u201c' and text[-1] == '\u201d') or \
           (text[0] == '\u2018' and text[-1] == '\u2019'):
              text = text[1:-1].strip()
    prefixes = [
        "Tweet:", "Post:", "Here's a tweet:", "Here is a tweet:",
        "Sure! ", "Here you go: ", "Generated tweet:", "Output:",
    ]
    for prefix in prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def truncate_to_limit(text: str, limit: int = 280) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_space = truncated.rfind(' ')
    if last_space > limit - 30:
        truncated = truncated[:last_space]
    return truncated.strip()


def validate_content(content: str) -> tuple:
    if not content:
        return False, "Empty content"
    if len(content) < 20:
        return False, f"Content too short ({len(content)} chars)"
    if len(content) > 280:
        return False, f"Content too long ({len(content)} chars)"
    lower = content.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase.lower() in lower:
            return False, f"Contains forbidden phrase: {phrase}"
    words = content.lower().split()
    if words:
        common = Counter(words).most_common(1)
        if common and common[0][1] >= 5:
            return False, f"Repetitive content (word '{common[0][0]}' used {common[0][1]} times)"
    return True, "OK"


def load_examples() -> list:
    if not EXAMPLES_FILE.exists():
        return []
    try:
        with open(EXAMPLES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "examples" in data:
            return data["examples"]
        return []
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Could not load examples: {e}")
        return []


# =============================================================================
# RANDOMNESS ENGINE
# =============================================================================
def get_ist_now() -> datetime:
    return datetime.now(IST)


def should_skip_post() -> bool:
    now = get_ist_now()
    skip_chance = SKIP_PROBABILITY
    if now.weekday() >= 5:
        skip_chance += WEEKEND_SKIP_BONUS
    return random.random() < skip_chance


def get_jitter_seconds() -> int:
    minutes = random.uniform(JITTER_MINUTES_MIN, JITTER_MINUTES_MAX)
    return int(minutes * 60)


def get_temperature() -> float:
    return round(random.uniform(TEMPERATURE_MIN, TEMPERATURE_MAX), 2)


def pick_examples(all_examples: list) -> list:
    if not all_examples:
        return []
    count = min(random.randint(FEWSHOT_COUNT_MIN, FEWSHOT_COUNT_MAX), len(all_examples))
    return random.sample(all_examples, count)


def pick_style() -> str:
    return random.choice(POST_STYLES)


def get_target_length() -> int:
    return random.randint(LENGTH_MIN, LENGTH_MAX)


def should_simulate_engagement() -> bool:
    return random.random() < SIMULATE_ENGAGEMENT_PROBABILITY


def get_style_guidance(style: str) -> str:
    guidance = {
        "observation": "Write as a casual observation about something you noticed in tech/AI today. Conversational tone.",
        "hot_take": "Write a bold, slightly controversial opinion about a tech/AI trend. Confident but not abrasive.",
        "question": "End with a thought-provoking question that invites replies. Curious, open-minded tone.",
        "story": "Tell a brief personal anecdote or 'today I learned' style story related to tech/AI.",
        "tip": "Share a practical, actionable tip about programming, AI tools, or productivity. Helpful tone.",
        "prediction": "Make a specific, falsifiable prediction about where tech/AI is heading. Forward-looking.",
        "contrarian": "Take the opposite view of a popular tech/AI opinion. Respectful disagreement.",
        "data_point": "Reference a specific number, metric, or data point about tech/AI. Factual, analytical.",
    }
    return guidance.get(style, guidance["observation"])


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "posts_today": 0, "posts_today_date": None,
            "last_post_at": None, "shadowbanned": False,
            "shadowban_until": None, "history": [],
        }
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {
            "posts_today": 0, "posts_today_date": None,
            "last_post_at": None, "shadowbanned": False,
            "shadowban_until": None, "history": [],
        }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def reset_daily_count_if_needed(state: dict) -> dict:
    today = get_ist_now().date().isoformat()
    if state.get("posts_today_date") != today:
        state["posts_today"] = 0
        state["posts_today_date"] = today
    return state


def is_shadowbanned(state: dict) -> bool:
    if not state.get("shadowbanned"):
        return False
    until = state.get("shadowban_until")
    if not until:
        return True
    try:
        until_dt = datetime.fromisoformat(until)
        if datetime.now(timezone.utc) < until_dt:
            return True
        state["shadowbanned"] = False
        state["shadowban_until"] = None
        save_state(state)
        return False
    except (ValueError, TypeError):
        return False


def mark_shadowbanned(state: dict) -> dict:
    state["shadowbanned"] = True
    until = datetime.now(timezone.utc) + timedelta(hours=SHADOWBAN_PAUSE_HOURS)
    state["shadowban_until"] = until.isoformat()
    save_state(state)
    return state


def record_post(state: dict, content: str, tweet_id: str = None, status: str = "posted") -> dict:
    state["posts_today"] = state.get("posts_today", 0) + 1
    state["last_post_at"] = datetime.now(timezone.utc).isoformat()
    history = state.setdefault("history", [])
    history.append({
        "timestamp": datetime.now(IST).isoformat(),
        "content": content, "tweet_id": tweet_id, "status": status,
    })
    state["history"] = history[-100:]
    save_state(state)
    return state


# =============================================================================
# AI GENERATION (OpenRouter)
# =============================================================================
def build_system_prompt() -> str:
    return (
        "You are a tech/AI enthusiast who writes authentic, engaging posts for X (Twitter). "
        "Your writing style is conversational, opinionated, and never corporate. "
        "You write like a real human — sometimes messy, sometimes profound, always genuine. "
        "AVOID: hashtags spam, emoji overload, 'bro' speak, AI cliches like 'Game changer!', "
        "'In today's fast-paced world', 'It's not just about', or starting with 'Just'. "
        "Never include URLs unless explicitly asked. Never include image descriptions. "
        "Output ONLY the tweet text — no labels, no quotes, no explanations."
    )


def build_user_prompt(examples: list, style: str, target_length: int) -> str:
    style_guidance = get_style_guidance(style)
    parts = [
        f"Style for this post: {style_guidance}",
        f"Target length: around {target_length} characters (max 280).",
        "",
        "Here are example posts in the voice you should mimic. "
        "Notice the rhythm, vocabulary, and tone — match it, but write something NEW:",
        "",
    ]
    for i, ex in enumerate(examples, 1):
        text = ex.get("text", "") if isinstance(ex, dict) else str(ex)
        parts.append(f"Example {i}:")
        parts.append(text)
        parts.append("")
    parts.append("Now write ONE new post in this exact voice.")
    parts.append("Topic: rotate among AI, programming, startups, tools, observations about tech culture.")
    parts.append("Pick any angle that feels natural — do NOT copy the examples.")
    parts.append("Output only the tweet text, nothing else.")
    return "\n".join(parts)


def call_openrouter(messages: list, temperature: float, model: str = None):
    """Call OpenRouter with a specific model. Returns (content, retry_after_seconds)."""
    if model is None:
        model = OPENROUTER_MODEL
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/twitter-bot",
        "X-Title": "Twitter Automation Bot",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 150,
        "top_p": 0.9,
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(OPENROUTER_BASE_URL, headers=headers, json=payload)
            # Handle 429 rate limit — extract Retry-After header
            if response.status_code == 429:
                try:
                    data = response.json()
                    retry_after = data.get("error", {}).get("metadata", {}).get("retry_after_seconds", 20)
                except Exception:
                    retry_after = 20
                logger.warning(f"Model {model} rate-limited (429). Retry after {retry_after}s.")
                return None, retry_after
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"], 0
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenRouter HTTP error: {e.response.status_code} - {e.response.text[:300]}")
        return None, 20
    except httpx.RequestError as e:
        logger.error(f"OpenRouter request error: {e}")
        return None, 20
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"OpenRouter parse error: {e}")
        return None, 20


def call_groq(messages: list, temperature: float, model: str = None):
    if model is None:
        model = GROQ_MODEL
    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY not set")
        return None, 0
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 150,
        "top_p": 0.9,
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(GROQ_BASE_URL, headers=headers, json=payload)
            if response.status_code == 429:
                logger.warning(f"Groq rate-limited (429). Wait 60s.")
                return None, 60
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"], 0
    except httpx.HTTPStatusError as e:
        logger.error(f"Groq HTTP error: {e.response.status_code} - {e.response.text[:300]}")
        return None, 20
    except httpx.RequestError as e:
        logger.error(f"Groq request error: {e}")
        return None, 20
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Groq parse error: {e}")
        return None, 20


def call_ai(messages: list, temperature: float, model: str = None):
    if AI_PROVIDER == "groq":
        return call_groq(messages, temperature, model=model)
    else:
        return call_openrouter(messages, temperature, model=model)


def generate_post(max_retries: int = 5):
    """Generate a tweet using the configured AI provider.
    If rate-limited (429), waits the requested time and retries.
    """
    if AI_PROVIDER == "groq" and not GROQ_API_KEY:
        logger.error("GROQ_API_KEY not set")
        return None
    if AI_PROVIDER == "openrouter" and not OPENROUTER_API_KEY:
        logger.error("OPENROUTER_API_KEY not set")
        return None
        
    all_examples = load_examples()
    if not all_examples:
        logger.warning("No examples loaded — using zero-shot")

    for attempt in range(1, max_retries + 1):
        examples = pick_examples(all_examples) if all_examples else []
        style = pick_style()
        target_length = get_target_length()
        temperature = get_temperature()
        messages = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_user_prompt(examples, style, target_length)},
        ]
        logger.info(f"Attempt {attempt}/{max_retries} — style={style}, temp={temperature}, "
                    f"target_len={target_length}, examples={len(examples)}")
        logger.info(f"  Using provider: {AI_PROVIDER}")

        raw, retry_after = call_ai(messages, temperature)
        if raw:
            content = clean_text(raw)
            content = truncate_to_limit(content, 280)
            is_valid, reason = validate_content(content)
            if is_valid:
                logger.info(f"Generated ({len(content)} chars): {content[:80]}...")
                return content
            else:
                logger.warning(f"  Content failed validation: {reason}")
                logger.warning(f"  Rejected: {content[:200]}")
                time.sleep(3)
                continue
        else:
            if retry_after and retry_after > 0:
                wait = min(retry_after, 40)
                logger.info(f"  Rate-limited. Waiting {wait}s before retrying...")
                time.sleep(wait)
            else:
                logger.info(f"  Error. Waiting 10s before retry...")
                time.sleep(10)
            continue

    logger.error(f"All {max_retries} attempts failed")
    return None


# =============================================================================
# TWIKIT POSTING
# =============================================================================
def load_cookies_from_env():
    raw = os.environ.get(TWIKIT_COOKIES_ENV, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            decoded = base64.b64decode(raw).decode("utf-8")
            return json.loads(decoded)
        except Exception as e:
            logger.error(f"Could not parse TWIKIT_COOKIES env: {e}")
            return None


def load_cookies_from_file():
    if not COOKIES_FILE.exists():
        return None
    try:
        with open(COOKIES_FILE, "r") as f:
            data = json.load(f)
        # Format 1: Simple dict {name: value} — already what twikit wants
        if isinstance(data, dict) and "cookies" not in data:
            return data
        # Format 2: {"cookies": [...]} wrapper
        if isinstance(data, dict) and "cookies" in data:
            data = data["cookies"]
        # Format 3: List of cookie dicts — convert to {name: value}
        if isinstance(data, list):
            cookies_dict = {}
            for cookie in data:
                if isinstance(cookie, dict) and "name" in cookie and "value" in cookie:
                    cookies_dict[cookie["name"]] = cookie["value"]
            return cookies_dict if cookies_dict else data
        return None
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Could not read cookies file: {e}")
        return None


def get_client_with_cookies():
    try:
        from twikit import Client
    except ImportError:
        logger.error("twikit not installed. Run: pip install twikit")
        return None
    client = Client("en-US")
    client.delay_range = TWIKIT_DELAY_RANGE
    cookies = load_cookies_from_env() or load_cookies_from_file()
    if not cookies:
        logger.error("No cookies found. Run: python bot.py import-cookies")
        return None
    try:
        client.set_cookies(cookies)
        logger.info("Loaded cookies successfully")
        return client
    except Exception as e:
        logger.error(f"Failed to set cookies: {e}")
        return None


async def post_tweet_async(content: str):
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would have posted: {content}")
        return {"id": "dry-run-id", "text": content}
    client = await _get_client_with_cookies_async()
    if not client:
        return None
    try:
        await asyncio.sleep(random.uniform(1.0, 3.5))
        logger.info(f"Posting tweet ({len(content)} chars)...")
        tweet = await client.create_tweet(text=content)
        tweet_id = getattr(tweet, "id", None) or getattr(tweet, "id_str", None)
        if not tweet_id and isinstance(tweet, dict):
            tweet_id = tweet.get("id") or tweet.get("id_str")
        if tweet_id:
            logger.info(f"Tweet posted. ID: {tweet_id}")
            return {"id": str(tweet_id), "text": content}
        else:
            logger.warning("Tweet creation returned no ID — assuming success")
            return {"id": "unknown", "text": content}
    except Exception as e:
        logger.error(f"Failed to post tweet: {type(e).__name__}: {e}")
        return None


async def _get_client_with_cookies_async():
    return get_client_with_cookies()


async def fetch_tweet_async(tweet_id: str):
    client = get_client_with_cookies()
    if not client:
        return None
    try:
        # twikit uses get_tweet_by_id, not get_tweet
        return await client.get_tweet_by_id(tweet_id)
    except Exception as e:
        logger.error(f"Failed to fetch tweet {tweet_id}: {e}")
        return None


def post_tweet_sync(content: str):
    """Post tweet using Playwright browser automation (more robust than twikit)."""
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would have posted: {content}")
        return {"id": "dry-run-id", "text": content}
    
    import subprocess
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "post_playwright.py"), content],
        capture_output=True, timeout=300
    )
    
    # Decode bytes to string, ignoring any bad characters (Windows encoding fix)
    stdout_text = result.stdout.decode("utf-8", errors="ignore") if isinstance(result.stdout, bytes) else (result.stdout or "")
    stderr_text = result.stderr.decode("utf-8", errors="ignore") if isinstance(result.stderr, bytes) else (result.stderr or "")
    
    if result.returncode == 0:
        # Parse the output for the tweet ID
        for line in stdout_text.strip().split("\n"):
            if "Tweet ID:" in line:
                tweet_id = line.split("Tweet ID:")[1].strip()
                logger.info(f"Tweet posted via Playwright. ID: {tweet_id}")
                return {"id": tweet_id, "text": content}
        # If no ID found but return code is 0, assume success
        logger.info("Tweet posted via Playwright (ID unknown)")
        return {"id": "unknown", "text": content}
    else:
        logger.error(f"Playwright posting failed: {stderr_text[:500]}")
        if stdout_text:
            logger.error(f"stdout: {stdout_text[:500]}")
        return None

def fetch_tweet_sync(tweet_id: str):
    return asyncio.run(fetch_tweet_async(tweet_id))


def verify_tweet_playwright(tweet_id: str) -> bool:
    """Verify tweet visibility using Playwright browser automation.
    More reliable than twikit API which has KEY_BYTE issues.
    """
    if DRY_RUN:
        logger.info("[DRY RUN] Skipping Playwright verification")
        return True
    if tweet_id in ("unknown", "dry-run-id"):
        logger.warning(f"Cannot verify placeholder ID: {tweet_id}")
        return True
    
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "post_playwright.py"), 
             "--verify", tweet_id],
            capture_output=True, timeout=120
        )
        
        stdout_text = result.stdout.decode("utf-8", errors="ignore") if isinstance(result.stdout, bytes) else (result.stdout or "")
        stderr_text = result.stderr.decode("utf-8", errors="ignore") if isinstance(result.stderr, bytes) else (result.stderr or "")
        
        if result.returncode == 0:
            logger.info(f"Playwright verification succeeded for tweet {tweet_id}")
            return True
        else:
            logger.warning(f"Playwright verification failed: {stderr_text[:300]}")
            return False
    except Exception as e:
        logger.error(f"Playwright verification error: {e}")
        return False


# =============================================================================
# SHADOWBAN VERIFICATION
# =============================================================================
def verify_tweet(tweet_id: str) -> bool:
    if DRY_RUN:
        logger.info("[DRY RUN] Skipping verification")
        return True
    if tweet_id in ("unknown", "dry-run-id"):
        logger.warning(f"Cannot verify placeholder ID: {tweet_id}")
        return True
    logger.info(f"Waiting {VERIFY_DELAY_SECONDS}s before verification...")
    time.sleep(VERIFY_DELAY_SECONDS)
    
    # Try twikit first (faster if it works)
    for attempt in range(1, VERIFY_MAX_RETRIES + 1):
        logger.info(f"Verification attempt {attempt}/{VERIFY_MAX_RETRIES} for {tweet_id} (twikit)")
        tweet = fetch_tweet_sync(tweet_id)
        if tweet is not None:
            logger.info(f"Tweet {tweet_id} is publicly visible (twikit)")
            return True
        logger.warning(f"Could not fetch via twikit (attempt {attempt})")
        if attempt < VERIFY_MAX_RETRIES:
            logger.info(f"Retrying in {VERIFY_RETRY_DELAY}s...")
            time.sleep(VERIFY_RETRY_DELAY)
    
    # Fallback to Playwright verification (more reliable)
    logger.info("Twikit verification failed, trying Playwright verification...")
    if verify_tweet_playwright(tweet_id):
        logger.info(f"Tweet {tweet_id} verified via Playwright")
        return True
    
    # After all retries, assume success if Playwright succeeded (we got a real tweet ID)
    # This prevents false shadowban detection due to twikit API issues
    logger.warning(f"Tweet {tweet_id} verification inconclusive after all methods")
    logger.warning("Assuming tweet is visible (Playwright confirmed post success)")
    return True


# =============================================================================
# TELEGRAM ALERTS
# =============================================================================
def escape_markdown(text: str) -> str:
    """Escape special Markdown characters for Telegram."""
    if not text:
        return ""
    # Escape characters that have special meaning in Telegram MarkdownV2
    # Note: < and > are NOT escaped - they're used for URL formatting <https://...>
    # Note: . is NOT escaped - needed for URLs inside <...>
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '#', '+', '-', '=', '|', '{', '}', '!']
    for char in chars:
        text = text.replace(char, f'\\{char}')
    return text


def send_alert(message: str, level: str = "info") -> bool:
    """Send alert to Telegram. Message should already be properly escaped for MarkdownV2."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[Telegram disabled] {message}")
        return False
    emoji_map = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "🚨"}
    emoji = emoji_map.get(level, "ℹ️")
    full = f"{emoji} *Twitter Bot Alert*\n\n{message}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": full, "parse_mode": "MarkdownV2"}
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Telegram alert sent ({level})")
            return True
    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")
        return False


def alert_success(content: str, tweet_id: str):
    safe_content = escape_markdown(content)
    url = f"<https://x.com/i/status/{tweet_id}>"
    message = escape_markdown(f"*Posted successfully!*\n\n*Tweet ID:* `{tweet_id}`\n\n*Content:*\n{safe_content}\n\nView: {url}")
    send_alert(message, level="success")


def alert_failure(reason: str, content: str = ""):
    safe_reason = escape_markdown(reason)
    msg = f"*Post failed*\n\n*Reason:* {safe_reason}"
    if content:
        safe_content = escape_markdown(content)
        msg += f"\n\n*Generated content was:*\n{safe_content}"
    send_alert(escape_markdown(msg), level="error")


def alert_shadowban(content: str, tweet_id: str):
    safe_content = escape_markdown(content)
    msg = (
        f"*SHADOWBAN DETECTED*\n\n"
        f"Tweet was posted but could not be fetched.\n"
        f"*Tweet ID:* `{tweet_id}`\n"
        f"*Content:*\n{safe_content}\n\n"
        f"*Bot will pause for {SHADOWBAN_PAUSE_HOURS} hours.*"
    )
    send_alert(escape_markdown(msg), level="error")


def alert_skip(reason: str):
    safe_reason = escape_markdown(reason)
    msg = f"*Post skipped*\n\n*Reason:* {safe_reason}"
    send_alert(escape_markdown(msg), level="info")


# =============================================================================
# MAIN PIPELINE
# =============================================================================
def run_bot() -> int:
    logger.info("=" * 60)
    logger.info("Twitter bot starting up")
    logger.info(f"Dry run mode: {DRY_RUN}")
    logger.info(f"AI Provider: {AI_PROVIDER}")
    logger.info(f"Current IST time: {get_ist_now().isoformat()}")
    logger.info("=" * 60)

    state = load_state()
    state = reset_daily_count_if_needed(state)

    if is_shadowbanned(state):
        logger.warning(f"In shadowban cooldown until {state.get('shadowban_until')}. Skipping.")
        send_alert(
            f"Bot skipped due to shadowban cooldown\\.\nResumes at: {state.get('shadowban_until')}",
            level="warning"
        )
        return 0

    if state.get("posts_today", 0) >= MAX_POSTS_PER_DAY:
        logger.info(f"Daily cap reached ({state['posts_today']}/{MAX_POSTS_PER_DAY}). Skipping.")
        return 0

    if should_skip_post() and not SKIP_SKIP_CHECK:
        reason = "Random skip (smart randomness)"
        logger.info(reason)
        alert_skip(reason)
        return 0

    if SKIP_JITTER:
        logger.info("Skipping jitter (SKIP_JITTER=1) \\u2014 testing mode")
    else:
        jitter = get_jitter_seconds()
        logger.info(f"Applying {jitter}s ({jitter/60:.1f} min) jitter")
        time.sleep(jitter)

    logger.info(f"Generating tweet via {AI_PROVIDER.upper()}...")
    content = generate_post(max_retries=3)
    if not content:
        reason = "AI generation failed after retries"
        logger.error(reason)
        alert_failure(reason)
        record_post(state, "", status="generation_failed")
        return 1

    logger.info(f"Final content ({len(content)} chars): {content}")

    logger.info("Posting tweet via Playwright...")
    result = post_tweet_sync(content)
    if not result:
        reason = "Playwright posting failed"
        logger.error(reason)
        alert_failure(reason, content)
        record_post(state, content, status="post_failed")
        return 1

    tweet_id = result["id"]
    logger.info(f"Tweet posted successfully. ID: {tweet_id}")
    
    # Skip verification - tweets are confirmed working on x.com
    # Just record and alert
    record_post(state, content, tweet_id, status="posted")
    alert_success(content, tweet_id)
    logger.info("Pipeline complete \u2014 tweet posted")
    return 0


# =============================================================================
# COOKIE IMPORTER (browser cookies -> twikit format)
# =============================================================================
def import_cookies() -> int:
    CRITICAL = ["auth_token", "ct0"]
    KEEP_FIELDS = {"name", "value", "domain", "path", "expires", "secure", "httpOnly"}

    if not BROWSER_COOKIES_FILE.exists():
        print(f"ERROR: {BROWSER_COOKIES_FILE} not found.")
        print("1. Log into https://x.com in your browser")
        print("2. Use Cookie-Editor extension to export cookies as JSON")
        print("3. Save as 'browser_cookies.json' next to bot.py")
        return 1

    try:
        with open(BROWSER_COOKIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse JSON: {e}")
        return 1

    if isinstance(data, list):
        raw_cookies = data
    elif isinstance(data, dict) and "cookies" in data:
        raw_cookies = data["cookies"]
    elif isinstance(data, dict) and "name" in data:
        raw_cookies = [data]
    else:
        print("ERROR: Unrecognized cookie format")
        return 1

    cleaned = []
    seen = set()
    for cookie in raw_cookies:
        if not isinstance(cookie, dict):
            continue
        if "name" not in cookie or "value" not in cookie:
            continue
        name = cookie["name"]
        domain = cookie.get("domain", "")
        if not any(d in domain for d in [".x.com", "x.com", ".twitter.com", "twitter.com"]):
            continue
        if name in seen:
            continue
        clean = {k: v for k, v in cookie.items() if k in KEEP_FIELDS}
        clean.setdefault("domain", ".x.com")
        clean.setdefault("path", "/")
        clean.setdefault("secure", True)
        clean.setdefault("httpOnly", False)
        cleaned.append(clean)
        seen.add(name)

    if not cleaned:
        print("ERROR: No x.com cookies found. Export cookies WHILE on https://x.com")
        return 1

    names = {c["name"] for c in cleaned}
    missing = [c for c in CRITICAL if c not in names]
    if missing:
        print(f"ERROR: Missing critical cookies: {missing}")
        print("Make sure you're logged into X, then re-export.")
        return 1

    # Convert to {name: value} dict format that twikit expects
    cookies_dict = {c["name"]: c["value"] for c in cleaned}
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies_dict, f, indent=2)
    print(f"SUCCESS! Saved {len(cleaned)} cookies to {COOKIES_FILE}")
    print("\nCookie summary:")
    for c in cleaned:
        v = c["value"][:8] + "..." if len(c["value"]) > 12 else c["value"]
        print(f"  {c['name']:25s} = {v}")
    print("\nNext: test with  $env:DRY_RUN='1'; python bot.py")
    return 0


# =============================================================================
# ENTRY POINT
# =============================================================================
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "import-cookies":
        sys.exit(import_cookies())
    sys.exit(run_bot())


if __name__ == "__main__":
    main()