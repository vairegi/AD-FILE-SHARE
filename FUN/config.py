"""FUN Bot 3 — environment-variable loader.

Bot 3 is fully independent of Bots 1–2: it has its own Render service, MongoDB,
and provider keys. Same env-var style as AD-FILE-SHARE (no hardcoded secrets).
"""
import os

def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default

# Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
OWNER_ID = _int("OWNER_ID", 0)
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
ADMIN_IDS = sorted(set(ADMIN_IDS + ([OWNER_ID] if OWNER_ID else [])))

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "fun_secret")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = _int("PORT", 8000)

# MongoDB (independent of AD-FILE-SHARE — separate Atlas cluster)
MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "fun_bot3")

# Personality
BOT_NAME = os.environ.get("BOT_NAME", "Cute Hub")
PERSONA = os.environ.get("PERSONA", "senpai")  # senpai|kawaii|custom

# Economy
WELCOME_TOKENS = _int("WELCOME_TOKENS", 5)
COLLECTION_PRICE = _int("COLLECTION_PRICE", 2)
DAILY_CHECKIN_REWARD = _int("DAILY_CHECKIN_REWARD", 3)
STREAK_PERIOD_DAYS = _int("STREAK_PERIOD_DAYS", 5)
STREAK_BONUS = _int("STREAK_BONUS", 1)
REFERRAL_INVITER_BONUS = _int("REFERRAL_INVITER_BONUS", 5)
REFERRAL_FRIEND_BONUS = _int("REFERRAL_FRIEND_BONUS", 3)
DEFAULT_TASK_REWARD = _int("DEFAULT_TASK_REWARD", 5)
TASK_COOLDOWN_SECONDS = _int("TASK_COOLDOWN_SECONDS", 24 * 3600)

# Delivery
AUTO_DELETE_AFTER_SECONDS = _int("AUTO_DELETE_AFTER_SECONDS", 10 * 60)
ALBUM_BATCH_SIZE = _int("ALBUM_BATCH_SIZE", 10)  # Telegram hard limit

# Cover hosts
TELEGRAPH_SHORT_NAME = os.environ.get("TELEGRAPH_SHORT_NAME", "FUN Covers")
TELEGRAPH_AUTHOR = os.environ.get("TELEGRAPH_AUTHOR", "FUN Bot")
COVER_PRIMARY_HOST = os.environ.get("COVER_PRIMARY_HOST", "https://telegra.ph/upload")
COVER_FALLBACK_HOST = os.environ.get("COVER_FALLBACK_HOST", "https://graph.org/upload")

# Membership
REQUIRED_CHANNEL_IDS = [int(x) for x in os.environ.get("REQUIRED_CHANNEL_IDS", "").split(",") if x.lstrip("-").isdigit()]

# Legacy secure-page encryption only. New chat setup needs no extra env key.
SESSION_ENCRYPTION_KEY = os.environ.get("SESSION_ENCRYPTION_KEY", "").strip()

# Cover preview mode: "slideshow" (default, swipeable) or "collage"
COVER_MODE = os.environ.get("COVER_MODE", "slideshow").lower()
if COVER_MODE not in ("slideshow", "collage"):
    COVER_MODE = "slideshow"

# Health
HEALTH_PATH = "/health"
