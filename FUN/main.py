"""Independent Bot 3 ASGI app. Render root FUN; uvicorn main:app --port $PORT.
Never run more than one worker with this single-process scanner/dispatcher.
"""
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, BotCommand
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters, Defaults)
import config
import db
from pathlib import Path
if (Path(__file__).parent / "handlers" / "__init__.py").exists():
    raise RuntimeError("Old FUN/handlers package detected. Delete FUN/handlers/, FUN/rich/, FUN/tests/ from GitHub before deploying the flat v1.1.0 folder.")
import handlers
import runtime
import userbot_setup

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
# INFO request URLs contain /bot<TOKEN>/method. Do not log them.
for name in ("httpx", "httpcore", "telethon"):
    logging.getLogger(name).setLevel(logging.CRITICAL)
log = logging.getLogger("fun.main")


def build_bot():
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing; configure it in Render, never in source code.")
    application = Application.builder().token(config.BOT_TOKEN).defaults(
        Defaults(parse_mode=None)).build()
    commands = {"start": handlers.cmd_start, "checkin": handlers.cmd_checkin,
        "help": handlers.cmd_help, "broadcast": handlers.cmd_broadcast, "covermode": handlers.cmd_covermode,
    "telegraph_token": handlers.cmd_telegraph_token,
        "retry_covers": handlers.cmd_retry_covers, "cover_status": handlers.cmd_cover_status,
        "test_hosts": handlers.cmd_test_hosts, "select_host": handlers.cmd_select_host,
        "warm_covers": handlers.cmd_warm_covers,
        "protect": handlers.cmd_protect,
        "check_in": handlers.cmd_checkin, "admin": handlers.cmd_admin,
        "addchannel": handlers.cmd_addchannel, "removechannel": handlers.cmd_removechannel,
        "channels": handlers.cmd_channels, "scan": handlers.cmd_scan,
        "setup_userbot": handlers.cmd_setup_userbot, "cancel": handlers.cmd_cancel, "userbot_status": handlers.cmd_userbot_status,
        "remove_userbot": handlers.cmd_remove_userbot, "botstatus": handlers.cmd_botstatus,
        "reqchannel": handlers.cmd_reqchannel, "addtask": handlers.cmd_addtask,
        "setprice": handlers.cmd_setprice, "stats": handlers.cmd_stats,
        "pending": handlers.cmd_pending, "collections": handlers.cmd_collections}
    for name, fn in commands.items():
        application.add_handler(CommandHandler(name, fn, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(handlers.on_button))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,
                                           handlers.on_private_message))
    return application


app_bot = None


@asynccontextmanager
async def lifespan(app):
    global app_bot
    if not config.WEBHOOK_SECRET or config.WEBHOOK_SECRET == "fun_secret":
        raise RuntimeError("Set WEBHOOK_SECRET to a unique random value in Render.")
    await db.connect()
    app_bot = build_bot()
    await app_bot.initialize()
    await app_bot.start()
    # getMe is already cached by PTB initialization. No manual username is required.
    config.BOT_USERNAME = app_bot.bot.username
    if config.RENDER_EXTERNAL_URL:
        await app_bot.bot.set_webhook(config.RENDER_EXTERNAL_URL + "/webhook",
            secret_token=config.WEBHOOK_SECRET, allowed_updates=["message", "callback_query"])
    await app_bot.bot.set_my_commands([
        BotCommand("start", "Open the cute hub"), BotCommand("checkin", "Daily tokens"), BotCommand("help", "How it works"),
        BotCommand("admin", "Owner setup and controls")])
    try:
        client = await userbot_setup.build_client()
        if client:
            await runtime.replace_userbot(client)
    except Exception:
        log.warning("Userbot not connected. Owner: use /setup_userbot or /userbot_status.")
    import broadcast
    await broadcast.recover_after_restart()
    runtime.start_jobs(app_bot.bot)
    try:
        yield
    finally:
        await runtime.stop_jobs()
        await app_bot.stop()
        await app_bot.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"ok": True, "service": "fun-bot3", "version": "random-history-broadcast-v1"}


@app.post("/webhook")
async def webhook(request: Request):
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.WEBHOOK_SECRET or not secrets.compare_digest(provided, config.WEBHOOK_SECRET):
        return Response(status_code=403)
    if app_bot is None:
        return Response(status_code=503)
    try:
        data = await request.json()
        update = Update.de_json(data, app_bot.bot)
    except Exception:
        return Response(status_code=400)
    # Application update_queue serializes updates without making Telegram wait
    # for a scan or multi-file delivery. Explicit /scan runs separately too.
    await app_bot.update_queue.put(update)
    return Response(status_code=200)


SECURITY_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"}


@app.get("/setup/userbot")
async def setup_page():
    return HTMLResponse(userbot_setup.SETUP_HTML, headers=SECURITY_HEADERS)


@app.post("/setup/userbot")
async def setup_submit(request: Request):
    try:
        size = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return JSONResponse({"message": "Invalid request length."}, status_code=400, headers=SECURITY_HEADERS)
    if size > 16000:
        return JSONResponse({"message": "Request too large."}, status_code=413, headers=SECURITY_HEADERS)
    # No cookies/auth by browser session: require the explicit bearer from the
    # owner-only expiring setup URL. Cross-origin callers are rejected as well.
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != config.RENDER_EXTERNAL_URL:
        return JSONResponse({"message": "Origin not allowed."}, status_code=403, headers=SECURITY_HEADERS)
    try:
        body = await request.body()
        if len(body) > 16000:
            return JSONResponse({"message": "Request too large."}, status_code=413, headers=SECURITY_HEADERS)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid setup request.")
        token = payload.get("token", "")
        if not await userbot_setup.validate_setup_token(token):
            return JSONResponse({"message": "Invalid or expired link. Run /setup_userbot again."}, status_code=403, headers=SECURITY_HEADERS)
        async with runtime_setup_lock:
            client = await userbot_setup.import_credentials(token, payload.get("api_id"),
                payload.get("api_hash", ""), payload.get("session", ""))
            await runtime.replace_userbot(client)
        return JSONResponse({"message": "✅ Session validated, encrypted and connected. Return to Telegram: /addchannel then /scan."}, headers=SECURITY_HEADERS)
    except ValueError as exc:
        return JSONResponse({"message": str(exc)}, status_code=400, headers=SECURITY_HEADERS)
    except Exception:
        # Never echo exceptions from a networking library that might contain credentials.
        return JSONResponse({"message": "Account connection failed. Check the session authorization and API credentials; stored credentials were not replaced."}, status_code=400, headers=SECURITY_HEADERS)


runtime_setup_lock = asyncio.Lock()


@app.post("/task/complete")
async def task_complete(request: Request):
    import providers
    q = request.query_params
    try:
        user_id, ts = int(q["user_id"]), int(q["ts"])
    except (KeyError, ValueError):
        return Response(status_code=400)
    result = await providers.verify_callback(user_id, q.get("task_id", ""), ts, q.get("sig", ""))
    if result == "ok" and app_bot:
        try:
            await app_bot.bot.send_message(user_id, "🎉 <b>Task verified!</b> Tokens added, Senpai~!", parse_mode="HTML")
        except Exception:
            pass
    return {"status": result}
