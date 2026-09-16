"""Entry point: ONE FastAPI process hosting BOTH Telegram bots.

Two python-telegram-bot Applications run in webhook mode inside this single
process. Telegram delivers updates to:

    POST /bot1/webhook   -> Bot 1 (gate bot)
    POST /bot2/webhook   -> Bot 2 (delivery bot)
    GET  /health         -> Render health check

Run locally:   uvicorn main:app --host 0.0.0.0 --port 8000
On Render:     uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update

import config
import db
from bot1 import build_bot1, schedule_daily
from bot2 import build_bot2, schedule_sweeper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
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
        # Bot 2
        await bot2.bot.set_my_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show all commands"),
            BotCommand("setautodelete", "Set auto-delete timer"),
        ])
    except Exception as exc:
        log.warning("set_my_commands failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Database + both bots
    await db.connect()
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
    schedule_sweeper(bot2)
    await _set_commands()
    log.info("Both bots are up.")

    yield

    # Shutdown
    try:
        await bot1.stop()
        await bot1.shutdown()
        await bot2.stop()
        await bot2.shutdown()
    finally:
        await db.close()


app = FastAPI(title="Telegram Video Bots", lifespan=lifespan)


def _secret_ok(request: Request, expected: str) -> bool:
    if not expected:
        return True
    return request.headers.get("x-telegram-bot-api-secret-token") == expected


@app.get("/health")
async def health():
    healthy = True
    try:
        await db.ping()
    except Exception:
        healthy = False
    return {"status": "ok" if healthy else "degraded", "database": healthy}


@app.post("/bot1/webhook")
async def bot1_webhook(request: Request):
    if not _secret_ok(request, config.BOT1_WEBHOOK_SECRET):
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, bot1.bot)
    await bot1.process_update(update)
    return Response(status_code=200)


@app.post("/bot2/webhook")
async def bot2_webhook(request: Request):
    if not _secret_ok(request, config.BOT2_WEBHOOK_SECRET):
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, bot2.bot)
    await bot2.process_update(update)
    return Response(status_code=200)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=False)
