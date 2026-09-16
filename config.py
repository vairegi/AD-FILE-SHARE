"""Central configuration. Everything is read from environment variables —
no secret is ever hardcoded in the source."""
import os

from dotenv import load_dotenv

load_dotenv()


def _clean_username(value: str) -> str:
    return (value or "").strip().lstrip("@")


def _int_list(raw: str):
    """Parse '111,222, 333' -> [111, 222, 333], ignoring junk."""
    out = []
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def _opt_int(raw: str):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


# ── Bots ──────────────────────────────────────────────────────
BOT1_TOKEN = os.getenv("BOT1_TOKEN", "").strip()
BOT2_TOKEN = os.getenv("BOT2_TOKEN", "").strip()
BOT1_USERNAME = _clean_username(os.getenv("BOT1_USERNAME", ""))
BOT2_USERNAME = _clean_username(os.getenv("BOT2_USERNAME", ""))

BOT1_WEBHOOK_SECRET = os.getenv("BOT1_WEBHOOK_SECRET", "").strip()
BOT2_WEBHOOK_SECRET = os.getenv("BOT2_WEBHOOK_SECRET", "").strip()

# ── Database ──────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "video_bots").strip()

# ── Admins ────────────────────────────────────────────────────
ADMIN_IDS = _int_list(os.getenv("ADMIN_IDS", ""))

# ── Channels (optional seed values) ───────────────────────────
DB_CHANNEL_ID = _opt_int(os.getenv("DB_CHANNEL_ID", ""))
POST_CHANNEL_ID = _opt_int(os.getenv("POST_CHANNEL_ID", ""))
FORCE_SUB_CHANNEL_ID = _opt_int(os.getenv("FORCE_SUB_CHANNEL_ID", ""))

# ── Render / webhook ──────────────────────────────────────────
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.getenv("PORT", "8000"))

# ── Shortener ─────────────────────────────────────────────────
SHORTENER_API_KEY = os.getenv("SHORTENER_API_KEY", "").strip()

# ── Telethon userbot (only for /scandb) ───────────────────────
API_ID = _opt_int(os.getenv("API_ID", ""))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_USER = os.getenv("SESSION_USER", "").strip()
