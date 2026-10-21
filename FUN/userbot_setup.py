"""Encrypted owner onboarding. No sessions/login codes in Telegram chats.
One-use setup tokens are stored as hashes; the browser bearer stays in the
URL fragment (not HTTP request/access-log query strings).
"""
import hashlib
import json
import os
import re
import secrets
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken
import config
import db
import session_health

KEY_HELP = ("⚠️ SESSION_ENCRYPTION_KEY is invalid. It must be a Fernet key, not "
    "your API hash or a password. Generate one on your trusted computer with:\n"
    "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
    "Paste its entire output into Render → Environment → SESSION_ENCRYPTION_KEY "
    "(no surrounding quotes), save, and redeploy. Keep a private backup; don't change a valid key used for stored sessions.")


def _fernet():
    return Fernet((config.SESSION_ENCRYPTION_KEY or "").strip().encode())


def configuration_error():
    try:
        _fernet()
        return None
    except (ValueError, TypeError):
        return KEY_HELP


def encrypt_session(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt_session(blob):
    return _fernet().decrypt(blob.encode()).decode()


async def save_session(session_string):
    await db.update_settings({"userbot_session": encrypt_session(session_string)})


async def load_credentials():
    settings = await db.get_settings()
    if settings.get("userbot_chat_credentials"):
        import chat_setup
        return await chat_setup.load_credentials()
    if settings.get("userbot_credentials"):
        try:
            return json.loads(decrypt_session(settings["userbot_credentials"]))
        except (InvalidToken, ValueError, TypeError):
            raise ValueError("Stored credentials cannot be decrypted. Restore the original encryption key or re-import your session.") from None
    if settings.get("userbot_session"):
        api_id = os.environ.get("API_ID", "")
        api_hash = os.environ.get("API_HASH", "")
        if not api_id or not api_hash:
            raise ValueError("Legacy session found but API_ID/API_HASH are missing. Open /setup_userbot to configure all three securely.")
        return {"session": decrypt_session(settings["userbot_session"]),
                "api_id": int(api_id), "api_hash": api_hash}
    return None


async def load_session() -> Optional[str]:
    credentials = await load_credentials()
    return credentials["session"] if credentials else None


def validate_credentials(api_id, api_hash, session):
    try:
        api_id = int(api_id)
        if api_id <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("API ID must be a positive integer from my.telegram.org.") from None
    api_hash = str(api_hash).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("API hash must be the 32-character hexadecimal API hash from my.telegram.org.")
    session = str(session).strip()
    try:
        from telethon.sessions import StringSession
        parsed = StringSession(session)
        if not parsed.auth_key:
            raise ValueError()
    except Exception:
        raise ValueError("Invalid Telethon StringSession. Import a previously generated, authorized session; not your bot token, API hash, password or login code.") from None
    return {"api_id": api_id, "api_hash": api_hash, "session": session}


async def connect_credentials(credentials, client_factory=None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    factory = client_factory or TelegramClient
    client = factory(StringSession(credentials["session"]),
                     credentials["api_id"], credentials["api_hash"])
    try:
        import asyncio
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
            raise ValueError("This session is not authorized or has been revoked. Create a new StringSession on your trusted computer.")
        return client
    except BaseException:
        await session_health.safe_disconnect(client)
        raise


async def build_client():
    # New chat-import credentials need no API_ID/API_HASH/SESSION_ENCRYPTION_KEY.
    settings = await db.get_settings()
    if (settings.get('userbot_health') or {}).get('state') == 'reauth_required':
        return None
    if not settings.get("userbot_chat_credentials") and configuration_error():
        return None  # old format can be replaced through /setup_userbot
    credentials = await load_credentials()
    if not credentials:
        return None
    return await connect_credentials(credentials)


async def remove_session():
    await db.update_settings({"userbot_chat_credentials": None, "userbot_session": None, "userbot_credentials": None,
                               "userbot_setup_waiting": None, 'userbot_health': None})
    await db.col("setup_tokens").delete_many({})


async def issue_setup_token():
    token = secrets.token_urlsafe(32)
    # New setup invalidates older outstanding links.
    await db.col("setup_tokens").delete_many({})
    await db.col("setup_tokens").insert_one({
        "_id": hashlib.sha256(token.encode()).hexdigest(),
        "owner_id": config.OWNER_ID, "expires_at": db.now_ts() + 600,
        "used": False})
    return token


async def validate_setup_token(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 100:
        return None
    return await db.col("setup_tokens").find_one({
        "_id": hashlib.sha256(token.encode()).hexdigest(),
        "owner_id": config.OWNER_ID, "used": False,
        "expires_at": {"$gt": db.now_ts()}})


async def import_credentials(token, api_id, api_hash, session, client_factory=None):
    if configuration_error():
        raise ValueError("Encryption key is missing or invalid. Fix SESSION_ENCRYPTION_KEY in Render first.")
    doc = await validate_setup_token(token)
    if not doc:
        raise ValueError("Setup link expired or was already used. Run /setup_userbot again.")
    credentials = validate_credentials(api_id, api_hash, session)
    client = await connect_credentials(credentials, client_factory)
    # A connect/auth failure does not overwrite the previous known-good session.
    consumed = await db.col("setup_tokens").update_one(
        {"_id": doc["_id"], "used": False, "expires_at": {"$gt": db.now_ts()}},
        {"$set": {"used": True}})
    if consumed.modified_count != 1:
        await client.disconnect()
        raise ValueError("Setup link was already used. Generate a new link.")
    try:
        await db.update_settings({"userbot_credentials": encrypt_session(json.dumps(credentials)),
                                  "userbot_chat_credentials": None, 'userbot_health': None,
                                  "userbot_session": None, "userbot_setup_waiting": None})
    except BaseException:
        await session_health.safe_disconnect(client)
        raise
    return client


SETUP_HTML = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FUN Secure Userbot Setup</title></head>
<body style="font:17px system-ui;max-width:620px;margin:40px auto;padding:20px">
<h1>🎀 Secure Userbot Setup</h1><p>Only use your own dedicated account. The owner-only link expires in 10 minutes. Never share it.</p>
<p>API ID and API hash: <a href="https://my.telegram.org" rel="noreferrer" target="_blank">my.telegram.org</a>. Import an existing Telethon StringSession. Do not enter your phone number, login code or Telegram password here.</p>
<form id="setup"><label>API ID <input name="api_id" type="number" required autocomplete="off"></label><br><br>
<label>API hash <input name="api_hash" type="password" required autocomplete="off"></label><br><br>
<label>StringSession<br><textarea name="session" rows="7" style="width:100%" required autocomplete="off"></textarea></label><br>
<button id="submit">Validate, encrypt and connect</button></form><p id="result" role="status"></p>
<script>
const token=location.hash.slice(1); history.replaceState(null,'',location.pathname);
const form=document.getElementById('setup'), result=document.getElementById('result'), btn=document.getElementById('submit');
if(!token){result.textContent='Missing setup link. Open a new link from /setup_userbot.';btn.disabled=true;}
form.onsubmit=async e=>{e.preventDefault();btn.disabled=true;result.textContent='Checking account connection…';
try{const values=Object.fromEntries(new FormData(form));values.token=token;
const response=await fetch('/setup/userbot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});
const data=await response.json();result.textContent=data.message||'Setup failed.';if(response.ok){form.reset();form.hidden=true;}else{btn.disabled=false;}}
catch(e){result.textContent='Connection interrupted. Check /userbot_status before trying again.';btn.disabled=false;}};
</script></body></html>'''
