"""session_manager.py — multi-account rotation.
Env: STRING_SESSIONS="sess1,sess2,..." (or single STRING_SESSION).
v52: extra sessions can also be added at RUNTIME from the control bot
(/addworker <session string>) — they are stored in Mongo (config.extra_sessions)
and loaded here at startup, and /addworker also attaches them LIVE via
add_session() (no redeploy, no restart). Every client carries a `source` tag:
"env" for env-var sessions, "bot" for Mongo-added ones.
The scraper runs POSTS_PER_ACCOUNT posts on one account, then rotates to the
next while the first rests. On FloodWait it rotates immediately. Progress
lives in MongoDB keyed by target channel, so the next account continues
exactly where the previous one stopped."""
from telethon import TelegramClient
from telethon.sessions import StringSession
from config import API_ID, API_HASH, SESSIONS


class SessionManager:
    def __init__(self):
        self._clients = []
        self._idx = 0

    async def start(self):
        for sess in SESSIONS:
            c = TelegramClient(StringSession(sess), API_ID, API_HASH)
            await c.start()
            c.source = "env"            # v52: came from a Render env var
            self._clients.append(c)
        return self._clients[0]

    async def add_session(self, sess, source="bot"):
        """v52: attach a NEW StringSession to the live pool (/addworker).
        Logs the account in, tags it with its source, appends it to the
        rotation and returns the connected client. Raises whatever login
        raises (bad/expired string, FloodWait, ...) — nothing is appended on
        failure, so a bad string can never wedge the pool."""
        c = TelegramClient(StringSession(sess), API_ID, API_HASH)
        await c.start()
        c.source = source
        self._clients.append(c)
        return c

    async def remove_session(self, client):
        """v52: detach a bot-added client from the live pool (/removeworker).
        Disconnects it and removes it from the rotation. Never removes the
        last remaining worker (the scraper needs at least one). Returns True
        when removed, False when unknown or it's the final client."""
        if client not in self._clients or len(self._clients) <= 1:
            return False
        idx = self._clients.index(client)
        try:
            await client.disconnect()
        except Exception:
            pass
        self._clients.pop(idx)
        if self._idx >= len(self._clients):
            self._idx = 0
        return True

    def sources(self):
        """v52: parallel list of source tags ('env'/'bot') for the live pool,
        same order as all()."""
        return [getattr(c, "source", "env") for c in self._clients]

    def count(self):
        return len(self._clients)

    def current(self):
        return self._clients[self._idx]

    def current_name(self):
        return f"acc{self._idx + 1}/{len(self._clients)}"

    def rotate(self):
        self._idx = (self._idx + 1) % len(self._clients)
        return self.current()

    def all(self):
        return list(self._clients)
