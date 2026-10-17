# FUN — Bot 3 (independent cute-persona media bot)

Bot 3 is **fully independent** of Bots 1–2: its own Render service, its own
MongoDB Atlas cluster, and its own provider keys. Only the repo is shared.

## Deploy (second Render account)
1. New **Web Service** from this repo.
2. **Root Directory: `FUN`**
3. **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Set env vars (below).

## Environment variables
| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | ✅ | from @BotFather |
| `BOT_USERNAME` | ✅ | no `@`, used for referral deep links |
| `OWNER_ID` | ✅ | your Telegram user id (userbot setup) |
| `ADMIN_IDS` | ✅ | comma-separated (owner auto-added) |
| `MONGO_URI` | ✅ | **separate** Atlas cluster |
| `MONGO_DB_NAME` | ✅ | e.g. `fun_bot3` |
| `WEBHOOK_SECRET` | ✅ | any random string |
| `RENDER_EXTERNAL_URL` | ✅ | e.g. `https://fun-bot3.onrender.com` |
| `SESSION_ENCRYPTION_KEY` | ✅ for userbot | Fernet key, kept OUT of MongoDB |
| `BOT_NAME` | optional | card branding (default `Cute Hub`) |
| `REQUIRED_CHANNEL_IDS` | optional | seed; changeable via `/reqchannel` |
| `API_ID` / `API_HASH` | userbot | for the Telethon client |
| `PORT` | auto | Render sets it |

## UptimeRobot
Add an **HTTP monitor** to `https://<your-url>/health` every **5 minutes**.
The endpoint is cheap (no DB, no media). Note: free Render still has a
750-instance-hour monthly cap and cold starts — pinging reduces idle
spin-down but cannot guarantee 100% uptime or exact deletion timing while
the service is down. Deletion jobs persist in MongoDB and resume on wake.

## Userbot onboarding (secure)
1. Set `SESSION_ENCRYPTION_KEY` in Render (generate a Fernet key).
2. In the bot DM: `/setup_userbot`
3. Send your **StringSession** in the private reply. The bot encrypts it
   (key never stored in Mongo), stores it, and deletes your message.

## Cover detection rule
* Photo or video whose **caption has ≥ 3 non-empty lines** → **cover**.
  (Duration does NOT matter — covers can be 7s, 10s, or 20s.)
* Captionless media (even a 9s video) or a ≤2-line caption → **delivery file**.
* Albums (same `grouped_id`) are judged as one unit.
* Covers are **excluded** from delivered files.
* Files before the first cover are held in `/pending` for owner review.

## Economy
* Welcome: **5 tokens** (once). Referred friend: **8** (5+3).
* Every collection: **2 tokens** (per-collection override via `/setprice`).
* `/checkin`: **+3** daily (IST midnight reset), **+1 extra on days 5,10,15…**;
  missed day resets the streak; same-day repeat shows time remaining.
* Referral: inviter **+5** after the friend passes the required-channel join;
  bot notifies the inviter. Never double-credits.
* Sponsor tasks: configurable, default **+5**, 24h per-task cooldown; credit
  only on a **signed provider callback** (`/task/complete`), never on tap.

## Delivery
* Files copied **server-side** from the DB channel in album batches of ≤10
  (Telegram limit). Original captions preserved. No re-download through Render.
* Zero-delivery → **full refund**; completion card shows the **actual** count.
* Delivered media auto-delete after **10 min**; completion card is kept.
* Unlocked drops are permanent; **Re-Stream is free**. Save = separate bookmark.

## Admin commands
`/admin` panel: `/addchannel`, `/removechannel`, `/channels`, `/reqchannel`,
`/addtask`, `/setprice`, `/scan`, `/pending`, `/setup_userbot`, `/stats`.

## Tests
```
python tests/test_bot3.py
```
Covers classifier rules, welcome/referral economics, check-in streak +
cooldown, unlock/membership/insufficient/refund/double-tap/re-stream,
deletion queue, sponsor-task signing + cooldown, cover-host fallback, and
bookmark toggle. All run offline against an in-memory Mongo mock.
