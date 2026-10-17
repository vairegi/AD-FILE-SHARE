"""Owner-only API ID -> API hash -> StringSession conversation.
No account passwords/codes or extra environment keys. Partial inputs live
only in process memory for ten minutes. Finished credentials use AES/Fernet
with a key derived from the EXISTING secret MongoDB URI and a random salt.
A database dump alone doesn't contain that URI/key; full Render access does.
Changing MONGO_URI requires importing credentials again. This is deliberate
and documented, not encryption with a key stored alongside its ciphertext.
"""
import base64
import json
import re
import secrets
import time
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import config
import db
import runtime
import userbot_setup
from locks import user_lock

_pending = {}
TTL = 600


def owner_private(update):
    user = getattr(update, "effective_user", None)
    message = getattr(update, "message", None)
    chat = getattr(message, "chat", None) or getattr(update, "effective_chat", None)
    return bool(user and config.OWNER_ID and user.id == config.OWNER_ID
                and message and getattr(chat, "type", None) == "private")


def _purge():
    for uid, state in list(_pending.items()):
        if state["expires_at"] <= time.monotonic():
            _pending.pop(uid, None)


def _cipher(salt):
    uri = (config.MONGO_URI or "").strip()
    if not uri:
        raise ValueError("MongoDB is not configured. Keep MONGO_URI in Render.")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
               info=b"FUN/userbot/chat-setup/v1").derive(uri.encode())
    return Fernet(base64.urlsafe_b64encode(key))


async def save_credentials(credentials):
    salt = secrets.token_bytes(32)
    data = json.dumps(credentials, separators=(",", ":")).encode()
    await db.update_settings({"userbot_chat_credentials": {
        "format": "mongo-uri-hkdf-v1", "salt": base64.urlsafe_b64encode(salt).decode(),
        "ciphertext": _cipher(salt).encrypt(data).decode()},
        "userbot_credentials": None, "userbot_session": None,
        "userbot_setup_waiting": None})
    await db.col("setup_tokens").delete_many({})


async def load_credentials():
    saved = (await db.get_settings()).get("userbot_chat_credentials")
    if not saved:
        return None
    try:
        if saved.get("format") != "mongo-uri-hkdf-v1":
            raise ValueError()
        salt = base64.urlsafe_b64decode(saved["salt"])
        return json.loads(_cipher(salt).decrypt(saved["ciphertext"].encode()))
    except Exception:
        raise ValueError("Saved userbot credentials could not be read. If you changed MONGO_URI, run /setup_userbot again.") from None


async def begin(update, context):
    if not owner_private(update):
        return
    async with user_lock(update.effective_user.id):
        _purge()
        _pending[update.effective_user.id] = {"step": "api_id", "expires_at": time.monotonic() + TTL}
        await update.message.reply_text(
            "🎀 <b>Let's connect your userbot, Senpai~!</b>\n\n"
            "<b>Step 1/3:</b> Send your <b>API ID</b> (numbers only).\n\n"
            "No Render API/session key variables are needed. Setup expires in 10 minutes. "
            "Use /cancel to stop. I'll try to delete sensitive replies, but deletion isn't guaranteed.",
            parse_mode="HTML")


async def cancel(update, context):
    if not owner_private(update):
        return
    async with user_lock(update.effective_user.id):
        _pending.pop(update.effective_user.id, None)
        await update.message.reply_text("🎀 Setup cancelled, Senpai~! Your saved userbot was not changed.")


async def _delete_input(message):
    try:
        await message.delete()
        return True
    except Exception:
        return False


async def consume(update, context):
    if not owner_private(update):
        return False
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return False  # Never treat commands as credentials.
    uid = update.effective_user.id
    async with user_lock(uid):
        state = _pending.get(uid)
        if not state:
            return False
        if state["expires_at"] <= time.monotonic():
            _pending.pop(uid, None)
            await _delete_input(update.message)
            await update.message.reply_text("⏳ Setup expired. Please start again with /setup_userbot.")
            return True
        deleted = await _delete_input(update.message)
        if not deleted:
            await update.message.reply_text("⚠️ I couldn't delete that message. Please delete your sensitive reply yourself.")
        if state["step"] == "api_id":
            if not text.isascii() or not text.isdigit() or len(text) > 10 or not 0 < int(text) < 2**31:
                await update.message.reply_text("🎀 API ID must be a positive number. Please send only your API ID, Senpai~!")
                return True
            state["api_id"] = int(text)
            state["step"] = "api_hash"
            await update.message.reply_text("🌸 <b>Step 2/3:</b> Send your <b>API hash</b> (32 hexadecimal characters).", parse_mode="HTML")
            return True
        if state["step"] == "api_hash":
            if not re.fullmatch(r"[0-9a-fA-F]{32}", text):
                await update.message.reply_text("🎀 That API hash doesn't look right. Send the 32-character API hash from my.telegram.org.")
                return True
            state["api_hash"] = text
            state["step"] = "session"
            await update.message.reply_text("🎀 <b>Step 3/3:</b> Send your existing <b>Telethon StringSession</b> from your laptop script.\n\nNot your bot token, login code or password.", parse_mode="HTML")
            return True
        client = None
        try:
            credentials = userbot_setup.validate_credentials(state["api_id"], state["api_hash"], text)
            await update.message.reply_text("🌸 Checking your account connection, Senpai~! Please wait...")
            client = await userbot_setup.connect_credentials(credentials)
            # Verify a user account, not merely an authorized bot session.
            import asyncio
            account = await asyncio.wait_for(client.get_me(), timeout=15)
            if not account or getattr(account, "bot", False):
                raise ValueError("This must be a Telegram user-account StringSession, not a bot session.")
            await save_credentials(credentials)
            await runtime.replace_userbot(client)
            client = None  # runtime now owns disconnect/shutdown responsibility
            _pending.pop(uid, None)
            await update.message.reply_text(
                "✅ <b>Userbot connected, Senpai~!</b>\n\nCredentials saved automatically. "
                "I'll reconnect after redeploys.\n\n"
                "• /userbot_status — check connection\n"
                "• /remove_userbot — disconnect and remove\n"
                "• /addchannel &lt;channel_id&gt; &lt;category&gt; — map a source\n"
                "• /scan &lt;channel_id&gt; — index it",
                parse_mode="HTML")
        except ValueError as exc:
            # Networking constructors may also raise ValueError; only relay
            # messages we generate ourselves, never arbitrary exception text.
            safe = ("API ID must be a positive integer from my.telegram.org.",
                "API hash must be the 32-character hexadecimal API hash from my.telegram.org.",
                "Invalid Telethon StringSession. Import a previously generated, authorized session; not your bot token, API hash, password or login code.",
                "This session is not authorized or has been revoked. Create a new StringSession on your trusted computer.",
                "This must be a Telegram user-account StringSession, not a bot session.",
                "MongoDB is not configured. Keep MONGO_URI in Render.")
            reason = str(exc) if str(exc) in safe else "Connection settings could not be validated. Check your API credentials and session."
            from ui import h
            await update.message.reply_text("⚠️ " + h(reason) + "\nSend the correct StringSession to retry, or /cancel.", parse_mode="HTML")
        except Exception:
            # RPC/network errors can contain sensitive arguments: do not echo/log them.
            await update.message.reply_text(
                "⚠️ Connection failed. Your existing saved userbot was not replaced unless the new credentials were already saved. "
                "Check your API ID/hash and account session. Use /setup_userbot to restart, "
                "or resend the session for a temporary network failure. No login code is needed.")
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        return True


async def status(update, context):
    if not owner_private(update):
        return
    saved = await db.get_settings()
    client = runtime.userbot
    connected = bool(client)
    if client and hasattr(client, "is_connected"):
        connected = bool(client.is_connected())
    authorized = False
    if connected:
        try:
            import asyncio
            authorized = bool(await asyncio.wait_for(client.is_user_authorized(), timeout=10))
        except Exception:
            authorized = False
    stored = bool(saved.get("userbot_chat_credentials") or saved.get("userbot_credentials") or saved.get("userbot_session"))
    _purge()
    state = _pending.get(update.effective_user.id)
    await update.message.reply_text(
        "🎀 <b>Userbot Status</b>\n"
        f"Connected: <code>{'yes' if connected and authorized else 'no'}</code>\n"
        f"Session stored: <code>{'yes' if stored else 'no'}</code>\n"
        f"Setup in progress: <code>{state['step'] if state else 'no'}</code>\n\n"
        "Use /setup_userbot to add/reconnect, or /remove_userbot to remove.\n"
        "No API_ID, API_HASH or SESSION_ENCRYPTION_KEY environment variables are required.", parse_mode="HTML")


async def remove(update, context):
    if not owner_private(update):
        return
    async with user_lock(update.effective_user.id):
        _pending.pop(update.effective_user.id, None)
        await runtime.disconnect_userbot()
        await userbot_setup.remove_session()
        await update.message.reply_text(
            "✅ Userbot disconnected and saved credentials removed, Senpai~!\n"
            "Use /setup_userbot to add it again. To revoke the account session itself, "
            "open Telegram Settings → Devices.")
