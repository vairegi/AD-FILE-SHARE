"""Entry point: ONE FastAPI process hosting BOTH Telegram bots.

Two python-telegram-bot Applications run in webhook mode inside this single
process. Telegram delivers updates to:

    POST /bot1/webhook   -> Bot 1 (gate bot)
    POST /bot2/webhook   -> Bot 2 (delivery bot)
    GET  /health         -> Render health check

Run locally:   uvicorn main:app --host 0.0.0.0 --port 8000
On Render:     uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update

import config
import db
import outbound
import root_index
import root_dispatch
from bot1 import build_bot1, schedule_daily, schedule_broadcast_sweeper
from bot2 import build_bot2, schedule_sweeper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# httpx INFO records include full Telegram bot tokens and shortener query keys.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("main")

bot1 = build_bot1()
bot2 = build_bot2()


async def _set_commands():
    """Register the menu-button command lists for both bots."""
    from telegram import BotCommand, BotCommandScopeChat
    try:
        # Bot 1 — everyone sees the basics
        await bot1.bot.set_my_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("browse", "Browse by category & genre"),
            BotCommand("help", "Show all commands"),
        ])
        # Bot 1 — admins additionally get the full admin panel in the menu
        from bot1_admin import COMMANDS
        admin_cmds = [BotCommand("start", "Start the bot"),
                      BotCommand("help", "Show all commands")]
        admin_cmds += [BotCommand(name, "admin") for name in sorted(COMMANDS)]
        for admin_id in config.ADMIN_IDS:
            try:
                await bot1.bot.set_my_commands(
                    admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as exc:
                log.warning("admin menu for %s failed: %s", admin_id, exc)
        # Bot 2 — everyone sees the basics
        await bot2.bot.set_my_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show all commands"),
        ])
        # Bot 2 — admins additionally get broadcast / delivery controls (v4.7)
        b2_admin = [BotCommand("start", "Start the bot"),
                    BotCommand("help", "Show all commands"),
                    BotCommand("broadcast", "Message every Bot 2 user"),
                    BotCommand("setautodelete", "Auto-delete timer"),
                    BotCommand("withfilemessages", "Deletion notice text"),
                    BotCommand("checkram", "RAM usage")]
        for admin_id in config.ADMIN_IDS:
            try:
                await bot2.bot.set_my_commands(
                    b2_admin, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as exc:
                log.warning("bot2 admin menu for %s failed: %s", admin_id, exc)
    except Exception as exc:
        log.warning("set_my_commands failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Database + both bots
    await db.connect()
    await root_index.ensure_indexes()
    await root_dispatch.ensure_indexes()
    await root_index.bootstrap()
    await bot1.initialize()
    await bot1.start()
    await bot2.initialize()
    await bot2.start()

    # 2. Register webhooks (only when a public URL is configured)
    base = config.RENDER_EXTERNAL_URL.rstrip("/")
    if base:
        try:
            await bot1.bot.set_webhook(
                f"{base}/bot1/webhook",
                secret_token=config.BOT1_WEBHOOK_SECRET or None,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            await bot2.bot.set_webhook(
                f"{base}/bot2/webhook",
                secret_token=config.BOT2_WEBHOOK_SECRET or None,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            log.info("Webhooks registered at %s", base)
        except Exception as exc:
            log.error("Failed to set webhooks: %s", exc)

    # 3. Background jobs
    await schedule_daily(bot1.job_queue)
    schedule_broadcast_sweeper(bot1)   # v4.0: deletes timed /broadcast copies
    schedule_sweeper(bot2)
    await _set_commands()
    log.info("Both bots are up. Patch: db-admins-only + resilient-shorteners-v1")

    root_index.start()
    root_dispatch.start(bot1, bot2)
    try:
        yield
    finally:
        await root_dispatch.stop()
        await root_index.stop()
        try:
            await bot1.stop()
            await bot1.shutdown()
            await bot2.stop()
            await bot2.shutdown()
        finally:
            try:
                await outbound.close()
            finally:
                await db.close()


app = FastAPI(title="Telegram Video Bots", lifespan=lifespan)


def _secret_ok(request: Request, expected: str) -> bool:
    if not expected:
        return True
    return request.headers.get("x-telegram-bot-api-secret-token") == expected


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"service": "root-bots-1-2", "status": "running"}


@app.get("/health")
async def health():
    from fastapi.responses import JSONResponse
    try:
        await asyncio.wait_for(db.ping(), timeout=2.0)
    except Exception:
        return JSONResponse({"status": "degraded", "database": False}, status_code=503)
    return {"status": "ok", "database": True}


async def _webhook(request, name, application, secret):
    if not _secret_ok(request, secret):
        return Response(status_code=403)
    slots = root_dispatch.ingress_slots()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return Response(status_code=503)  # Telegram retries; nothing accepted/lost
    try:
        import json
        body = bytearray()
        declared = request.headers.get('content-length', '')
        if declared.isdigit() and int(declared) > root_dispatch.MAX_BODY_BYTES:
            return Response(status_code=413)
        async for chunk in request.stream():
            if len(body) + len(chunk) > root_dispatch.MAX_BODY_BYTES:
                return Response(status_code=413)
            body.extend(chunk)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(data.get('update_id'), int):
                return Response(status_code=400)
            update = Update.de_json(data, application.bot)
        except (ValueError, TypeError, KeyError):
            return Response(status_code=400)
        if name == 'bot1' and (update.channel_post or update.edited_channel_post):
            await root_index.stage_message(update.channel_post or update.edited_channel_post, update.update_id)
        else:
            await root_dispatch.enqueue(name, data)
        return Response(status_code=200)  # only after durable persistence succeeded
    except Exception as exc:
        log.warning('Webhook persistence retry bot=%s error=%s', name, type(exc).__name__)
        return Response(status_code=503)
    finally:
        slots.release()


@app.post("/bot1/webhook")
async def bot1_webhook(request: Request):
    return await _webhook(request, 'bot1', bot1, config.BOT1_WEBHOOK_SECRET)


@app.post("/bot2/webhook")
async def bot2_webhook(request: Request):
    return await _webhook(request, 'bot2', bot2, config.BOT2_WEBHOOK_SECRET)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=False)
