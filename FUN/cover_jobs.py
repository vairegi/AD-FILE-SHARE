"""Explicit cover backfill/retry job; doesn't block Telegram's dispatcher.
Only run by an owner/admin command in the deployed app. Metadata progress
survives restarts; successful per-image caches survive partial jobs.
"""
import asyncio
import logging
import httpx
import db
import covers
import runtime
log = logging.getLogger("fun.cover_jobs")
_task = None
QUERY = {"$or":[{"cover.url_status":{"$ne":"ok"}},
                 {"cover.url":None},{"cover.url":""}]}


def busy():
    return _task is not None and not _task.done()


def start(userbot, message=None, reply=None):
    global _task
    if busy(): return _task
    _task = runtime.spawn(_run(userbot,message,reply))
    return _task


async def _progress(message, text):
    if not message: return
    edit = getattr(message,"edit_text",None) or getattr(message,"edit_message_text",None)
    if edit:
        try:
            await edit(text)
        except Exception:
            pass


async def _run(userbot, message, reply):
    state={"total":await covers.needs_preparation_count(),"processed":0,
           "hosted":0,"local_ready":0,"failed":0,"state":"running"}
    await db.col("cover_jobs").update_one({"_id":"current"},{"$set":state},upsert=True)
    try:
        # No userbot credentials or delivery-file metadata are loaded here.
        docs = db.col("collections").find(QUERY,{"collection_id":1,"cover":1,"channel_id":1})
        async with httpx.AsyncClient() as http:
            async for doc in docs:
                try:
                    result = await covers.prepare_collection(userbot,doc,http,force=True)
                    if result["hosted"]: state["hosted"]+=1
                    elif result["ready"]: state["local_ready"]+=1
                    else: state["failed"]+=1
                except Exception:
                    state["failed"]+=1
                    await db.col("collections").update_one({"collection_id":doc["collection_id"]},
                        {"$set":{"cover.url_status":"failed","cover.preview_status":"failed",
                                 "cover.last_error":"preparation_failed_check_source_access"}})
                state["processed"]+=1
                await db.col("cover_jobs").update_one({"_id":"current"},{"$set":state})
                if state["processed"] % 10 == 0:
                    await _progress(message,f"🎀 Cover preparation: {state['processed']}/{state['total']} processed; "
                        f"{state['hosted']} hosted; {state['local_ready']} cached local; {state['failed']} unavailable.")
        state["state"]="complete"
        text=(f"✅ Cover preparation finished: {state['processed']}/{state['total']}.\n"
              f"Hosted links: {state['hosted']}\nCached images available for preview URLs: {state['local_ready']}\n"
              f"Source/preparation failures: {state['failed']}\n"
              "Open /start → Start Watching. Use /cover_status for totals.")
        await _progress(message,text)
        if reply and not (message and (hasattr(message,"edit_text") or hasattr(message,"edit_message_text"))):
            await reply(text)
    except asyncio.CancelledError:
        state["state"]="interrupted"
        raise
    except Exception:
        state["state"]="failed"
        log.warning("Cover backfill interrupted; cached assets remain available")
        if reply:
            await reply("⚠️ Cover preparation stopped. Check /cover_status; run /retry_covers to resume. Cached successes are kept.")
    finally:
        await db.col("cover_jobs").update_one({"_id":"current"},{"$set":state},upsert=True)


async def statistics():
    col = db.col("collections")
    result={"Collections":await col.count_documents({}),
        "Hosted URL ready":await col.count_documents({"cover.url_status":"ok","cover.url":{"$type":"string","$ne":""}}),
        "Telegraph links ready":await col.count_documents({"cover.url_status":"ok","cover.url":{"$regex":"^https?://(telegra.ph|graph.org)/"}}),
        "Feed mode":"in-place edit; Telegraph link preview, cached-image fallback",
        "Pending":await col.count_documents({"cover.url_status":"pending"}),
        "No status yet":await col.count_documents({"cover.url_status":{"$exists":False}}),
        "Mirror upload failed":await col.count_documents({"cover.url_status":"failed"}),
        "Cached cover assets ready":await col.count_documents({"cover.asset_keys.0":{"$exists":True}}),
        "Cached images":await db.col("cover_assets").count_documents({"data":{"$exists":True}}),
        "Worker running":"yes" if busy() else "no"}
    state=await db.col("cover_jobs").find_one({"_id":"current"})
    if state:
        result["Last job"]=f"{state.get('processed',0)}/{state.get('total',0)}; {state.get('state','unknown')}"
    last=await col.find_one({"cover.last_error":{"$exists":True,"$nin":[None,""]}},
        {"collection_id":1,"cover.last_error":1},sort=[("cover.last_attempt_at",-1)])
    if last:
        result["Last diagnostic"]=(last.get("cover") or {}).get("last_error")
    return result
