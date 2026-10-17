"""Sponsor-task provider callback verification.

A timer/link click is NOT proof of completion. Crediting happens only on a
server-side callback carrying an HMAC-SHA256 signature over
"{user_id}:{task_id}:{ts}" using the provider's per-bot secret (independent
Bot 3 keys, configured by the owner). Signatures expire after 10 minutes.
"""
import hashlib
import hmac
import time
from typing import Optional

import db

SIG_TTL_SECONDS = 600


def make_signature(secret: str, user_id: int, task_id: str, ts: int) -> str:
    msg = f"{user_id}:{task_id}:{ts}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


async def verify_callback(user_id: int, task_id: str, ts: int,
                          signature: str) -> str:
    """Returns 'ok' | 'cooldown' | 'unknown_task' | 'bad_signature' | 'expired'."""
    if abs(time.time() - ts) > SIG_TTL_SECONDS:
        return "expired"
    s = await db.get_settings()
    task = next((t for t in (s.get("tasks") or [])
                 if t.get("task_id") == task_id), None)
    if not task or not task.get("secret"):
        return "unknown_task"
    expected = make_signature(task["secret"], user_id, task_id, ts)
    if not hmac.compare_digest(expected, signature):
        return "bad_signature"
    from handlers import credit_task
    return await credit_task(user_id, task_id)
