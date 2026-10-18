"""Server-side source copying and persistent cleanup.
copyMessages preserves EXISTING source albums; it does not invent albums
from separately posted messages. Timeouts are uncertain, never blindly retried.
"""
import asyncio
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
import config
import db
from locks import channel_lock


async def deliver_collection(bot, user_id, collection, files, delivery_id=None):
    sent, failed = [], []
    protect = bool((await db.get_settings()).get("protect_content", True))
    channel = collection.get("channel_id")
    valid = [f for f in files if f.get("message_id")]
    for i in range(0, len(valid), max(1, min(10, config.ALBUM_BATCH_SIZE))):
        batch = valid[i:i + max(1, min(10, config.ALBUM_BATCH_SIZE))]
        ids = [f["message_id"] for f in batch]
        try:
            if len(ids) == 1:
                result = [await bot.copy_message(chat_id=user_id, from_chat_id=channel, message_id=ids[0], protect_content=protect)]
            else:
                result = await bot.copy_messages(chat_id=user_id, from_chat_id=channel, message_ids=ids, protect_content=protect)
            sent.extend(m.message_id for m in result)
            if len(result) != len(batch):
                # copyMessages may silently skip unavailable source messages.
                # It doesn't return a mapping, so don't guess which ones.
                failed.append({"message_ids": ids, "uncertain": True,
                               "reason": "Source-copy result count mismatch; owner reconciliation required"})
        except RetryAfter:
            failed.extend({**f, "reason": "rate_limited"} for f in batch)
        except (BadRequest, Forbidden):
            # Explicit rejection: safe to attempt individual source messages.
            for f in batch:
                try:
                    message = await bot.copy_message(chat_id=user_id, from_chat_id=channel, message_id=f["message_id"], protect_content=protect)
                    sent.append(message.message_id)
                except (BadRequest, Forbidden, RetryAfter):
                    failed.append({**f, "reason": "explicitly_rejected"})
                except Exception:
                    failed.append({**f, "uncertain": True, "reason": "network_or_unknown"})
        except Exception:
            # A network error can occur after Telegram accepted the request.
            failed.extend({**f, "uncertain": True, "reason": "network_or_unknown"} for f in batch)
        if delivery_id:
            await db.update_delivery(delivery_id, {"message_ids": list(sent), "failed_files": list(failed),
                "delete_status": "pending" if sent else "none"})
    return sent, failed


async def run_deletion_sweeper(bot):
    for delivery in await db.due_deletions():
        remaining = []
        for mid in delivery.get("message_ids") or []:
            try:
                await bot.delete_message(chat_id=delivery["user_id"], message_id=mid)
            except BadRequest as exc:
                # Only known terminal cases are considered finished.
                text = str(exc).lower()
                if "message to delete not found" not in text and "message can't be deleted" not in text:
                    remaining.append(mid)
            except Exception:
                remaining.append(mid)
        await db.update_delivery(delivery["delivery_id"], {
            "message_ids": remaining, "delete_status": "pending" if remaining else "done"})
