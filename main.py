"""Bot 3 entry point: ONE FastAPI process, webhook mode.

    POST /webhook  -> Bot 3 updates
    GET  /health   -> UptimeRobot monitor (lightweight, no DB/media work)
    POST /task/complete -> signed provider callback -> credit tokens

Deploy on Render with Root Directory = FUN, Start Command:
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)

import config
import db
import handlers

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("fun.main")

if not config.BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not configured")

app_bot = (Application.builder().token(config.BOT_TOKEN).build())
app_bot.add_handler(CommandHandler("start", handlers.cmd_start))
app_bot.add_handler(CommandHandler(["checkin", "check_in"], handlers.cmd_checkin))
app_bot.add_handler(CommandHandler("admin", handlers.cmd_admin))
app_bot.add_handler(CommandHandler("addchannel", handlers.cmd_addchannel))
app_bot.add_handler(CommandHandler("removechannel", handlers.cmd_removechannel))
app_bot.add_handler(CommandHandler("channels", handlers.cmd_channels))
app_bot.add_handler(CommandHandler("scan", handlers.cmd_scan))
app_bot.add_handler(CommandHandler("setup_userbot", handlers.cmd_setup_userbot))
app_bot.add_handler(CommandHandler("reqchannel", handlers.cmd_reqchannel))
app_bot.add_handler(CommandHandler("addtask", handlers.cmd_addtask))
app_bot.add_handler(CommandHandler("setprice", handlers.cmd_setprice))
app_bot.add_handler(CommandHandler("stats", handlers.cmd_stats))
app_bot.add_handler(CommandHandler("pending", handlers.cmd_pending))
app_bot.add_handler(CallbackQueryHandler(handlers.on_button))
app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   handlers.on_private_text))

_userbot_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _userbot_client
    await db.connect()
    await app_bot.initialize()
    await app_bot.start()
    if config.RENDER_EXTERNAL_URL:
        try:
            await app_bot.bot.set_webhook(
                f"{config.RENDER_EXTERNAL_URL}/webhook",
                secret_token=config.WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"])
        except Exception as exc:
            log.warning("set_webhook failed: %s", exc)
    try:
        import userbot_setup
        _userbot_client = await userbot_setup.build_client()
        log.info("userbot: %s", "connected" if _userbot_client else "no session")
    except Exception as exc:
        log.warning("userbot init: %s", exc)
    yield
    try:
        if _userbot_client:
            await _userbot_client.disconnect()
    finally:
        await app_bot.stop()
        await app_bot.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    """UptimeRobot ping target — intentionally cheap (no DB, no media)."""
    return {"ok": True, "service": "fun-bot3"}


@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    await app_bot.process_update(Update.de_json(data, app_bot.bot))
    return Response(status_code=200)


@app.post("/task/complete")
async def task_complete(request: Request):
    import providers
    q = request.query_params
    try:
        user_id, ts = int(q["user_id"]), int(q["ts"])
    except (KeyError, ValueError):
        return Response(status_code=400)
    result = await providers.verify_callback(
        user_id, q.get("task_id", ""), ts, q.get("sig", ""))
    if result == "ok":
        try:
            await app_bot.bot.send_message(user_id, "🎉 Task verified! Tokens added, Senpai~!")
        except Exception:
            pass
        return {"status": "ok"}
    return {"status": result}
