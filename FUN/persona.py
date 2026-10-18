"""HTML cards: bold headings/labels, monospace values, italic cute narration.
All dynamic strings are escaped HERE; callers must pass raw strings.
"""
import config
import random
from ui import h
SEP = "━━━━━━━━━━━━━━━━━━━━"


def head(text):
    return f"<b>{h(text)}</b>\n{SEP}\n"


def stat(icon, label, value):
    return f"{icon} <b>{h(label)}:</b> <code>{h(value)}</code>\n"


def cute(text):
    return f"<i>{h(text)}</i>"


def home(balance, streak, unlocked, saved):
    return (head(f"🎀 Uwahh~! Welcome to {config.BOT_NAME}! (⁄ ⁄•⁄ω⁄•⁄ ⁄)✨")
        + stat("👛", "Token Balance", f"{balance} Tokens")
        + stat("🔥", "Daily Streak", f"{streak} Days")
        + stat("🔓", "Unlocked Vault", f"{unlocked} Drops")
        + stat("⭐", "Saved Bookmarks", f"{saved} Posts") + SEP + "\n"
        + alive("home"))


def categories(balance):
    return (head("🎀 Explore Categories, Senpai~! (｡♥‿♥｡)✨")
        + stat("👛", "Token Balance", f"{balance} Tokens")
        + f"\nEvery drop costs <code>{config.COLLECTION_PRICE}</code> tokens.\n{SEP}\n"
        + cute("🌸 Uwahh, tap a shelf and I'll fetch the perfect album for you!"))


def feed_empty():
    return (head("🎀 E-Eh? This shelf is empty, Senpai... (´；ω；`)")
        + cute("🌸 New drops are being prepared — check back soon, okay?"))


def feed_caption(title, files, views, unlocks, price, balance, unlocked):
    """New layout: bold title, compact two-stat lines, brand footer.
    Unlocked state swaps the footer line for a free re-stream notice."""
    footer = (f"✅ <b>Already in your Vault</b> — Re-Stream is FREE!\n{SEP}\n"
              if unlocked else
              f"✨ <b>COLLECTION BY {h(config.BOT_NAME)}</b> ✨\n{SEP}\n")
    return (f"<b>🎀 New Drop જ⁀➴</b>\n{SEP}\n"
        + f"<b>{h(title)}</b>\n\n"
        + f"📦 <code>{h(files)}</code> <b>media files</b>\n"
        + f"👁 <b>Views:</b> <code>{h(views)}</code>  •  ❤️ <b>Unlocks:</b> <code>{h(unlocks)}</code>\n"
        + f"🪙 <b>Cost:</b> <code>{h(price)} tokens</code>  •  👛 <b>Balance:</b> <code>{h(balance)}</code>\n\n"
        + footer)


def insufficient(price, balance):
    return (head("🎀 A-Ahh, not enough tokens, Senpai! (；￣Д￣)")
        + stat("🪙", "Cost", f"{price} tokens") + stat("👛", "Token Balance", f"{balance} Tokens")
        + "\n" + cute("🌸 Tap Earn Tokens and I'll help you earn some more!"))


def stream_complete(title, delivered, price_paid):
    return (head("🎀 Stream Complete, Senpai~! (*≧ω≦)✨")
        + f"<blockquote>✨ COLLECTION BY {h(config.BOT_NAME)} ✨</blockquote>\n"
        + f"🎬 <b>{h(title)}</b>\n\n"
        + f"📦 Delivered <code>{delivered}</code> items • Auto-deletes in "
        f"<code>{config.AUTO_DELETE_AFTER_SECONDS // 60} min</code> ⏳\n{SEP}\n"
        + alive("delivery"))


def delivery_failed_refund(price, balance):
    return (head("🎀 G-Gomen, Senpai! The delivery failed... (╥﹏╥)")
        + stat("💸", "Refunded", f"{price} tokens")
        + stat("👛", "Token Balance", f"{balance} Tokens") + "\n"
        + cute("🌸 No files were delivered. Please try Unlock again, okay?"))


def help_card():
    return (head("🎀 Cute Assistant's Guide & Manual (⁄ ⁄•⁄ω⁄•⁄ ⁄)✨")
        + cute("🌸 U-Uwahh, don't worry if you get lost, Senpai! I'll explain everything! (*≧ω≦)")
        + "\n\n<b>1. Instant Media Stream 🎬</b>\n"
        "• Start Watching opens the feed.\n• Previous / Next changes the current card.\n"
        "• Categories filters the shelf.\n\n<b>2. Unlock &amp; Vault 📦</b>\n"
        f"• Each new collection costs <code>{config.COLLECTION_PRICE}</code> tokens.\n"
        "• Purchased collections stay in your Vault; Re-Stream is free.\n"
        f"• Delivered files auto-clean after <code>{config.AUTO_DELETE_AFTER_SECONDS // 60} min</code>.\n"
        "• Save adds a separate bookmark.\n\n<b>3. Free Token Rewards 👛</b>\n"
        f"• /checkin: <code>+{config.DAILY_CHECKIN_REWARD}</code> daily; streak bonuses apply.\n"
        "• Configured sponsor tasks have a 24-hour cooldown.\n"
        f"• Referrals: you <code>+{config.REFERRAL_INVITER_BONUS}</code>, friend "
        f"<code>+{config.REFERRAL_FRIEND_BONUS}</code>.\n{SEP}\n"
        + cute("🌸 If you need me, just tap Main Menu below, okay? Ehehe~"))


def profile(user_id, balance, streak, unlocked, saved, earned, spent, tasks, referrals):
    return (head("🎀 Senpai's Profile & Vault (⁄ ⁄•⁄ω⁄•⁄ ⁄)✨")
        + stat("🆔", "Account ID", user_id) + stat("👛", "Token Balance", f"{balance} Tokens")
        + stat("🔥", "Daily Streak", f"{streak} Days")
        + "\n<b>Your Personal Collection</b>\n"
        + stat("🔓", "Unlocked Vault", f"{unlocked} Collections")
        + stat("⭐", "Saved Bookmarks", f"{saved} Posts")
        + "\n<b>Economy Telemetry</b>\n"
        + stat("🎁", "Total Earned", f"{earned} Tokens")
        + stat("🪙", "Total Spent", f"{spent} Tokens")
        + stat("✅", "Tasks Solved", tasks) + stat("💞", "Friends Referred", referrals)
        + SEP + "\n" + cute("🌸 Your unlocked drops stay in your Vault. Re-stream anytime for free! (*≧ω≦)"))


def earn_home(balance):
    return (head("🎀 Earn Free Tokens, Senpai~! (*≧ω≦)✨")
        + stat("👛", "Token Balance", f"{balance} Tokens")
        + "\n<b>How to earn free tokens</b>\n"
        "1. Tap an available sponsor task.\n2. Complete its verification flow.\n"
        "3. Return to collect the configured reward.\n\n"
        + cute("🌸 Ehehe, each task renews every 24 hours!"))


def task_unavailable():
    return (head("🎀 No sponsor tasks yet, Senpai... (｡•́︿•̀｡)")
        + cute("🌸 The owner hasn't configured working sponsor tasks yet. Try /checkin or invite friends!"))


def task_cooldown(hours, minutes):
    return (head("⏳ Task Cooling Down!") + f"Come back in <code>{hours}h {minutes}m</code>.\n\n"
        + cute("🌸 Meanwhile, try /checkin or invite friends!"))


def checkin_done(base, bonus, balance, streak, next_bonus_in):
    text = head("✅ Daily Check-In Complete!") + stat("🎁", "Base Reward", f"+{base} tokens")
    if bonus:
        text += stat("🔥", "Streak Bonus", f"+{bonus} token")
    return (text + stat("💰", "Total Earned", f"+{base + bonus} tokens")
        + stat("🪙", "Balance", f"{balance} tokens") + "\n"
        + stat("🔥", "Streak", f"{streak} days")
        + f"📈 Next streak bonus in <code>{next_bonus_in} day(s)</code> (+{config.STREAK_BONUS} token)\n\n" + alive("checkin"))


def checkin_already(hours, minutes):
    return (head("⏳ Already Checked In!")
        + f"Come back in <code>{hours}h {minutes}m</code> for your next reward.\n\n"
        + cute("💡 Meanwhile, earn more tokens via shortener tasks!"))


def referral(link, friends, earned):
    return (head("🎀 INVITE FRIENDS & SHARE")
        + f"Share your personal link to invite friends:\n<a href=\"{h(link)}\">{h(link)}</a>\n\n"
        + "<b>Rewards</b>\n"
        + f"• You get: <code>+{config.REFERRAL_INVITER_BONUS}</code> Free Tokens per qualified friend\n"
        + f"• Friend gets: <code>+{config.REFERRAL_FRIEND_BONUS}</code> Bonus Tokens on joining!\n\n"
        + stat("💞", "Your Referrals", f"{friends} Friends")
        + stat("🎁", "Referral Earnings", f"+{earned} Tokens") + SEP + "\n"
        + cute("🌸 Tap Share on Telegram to send your invitation, Senpai~!"))


def referral_notify(friend_name, bonus, balance):
    return (head(f"🎉 Yatta! Your friend {friend_name} joined and passed the channel check!")
        + stat("🎁", "Referral Reward", f"+{bonus} tokens")
        + stat("🪙", "Updated Balance", f"{balance} Tokens")
        + "\n" + alive("referral"))


def join_required(channels):
    lines = "\n".join(f"• <code>{h(c)}</code>" for c in channels)
    return (head("🎀 W-Wait, Senpai! (｡•́︿•̀｡)")
        + "Join the required channel(s) before unlocking or re-streaming:\n" + lines
        + "\n\n" + cute("🌸 Join, then tap I've Joined — I'll be waiting!"))


SHARE_LINES = (
    "🎀 Psst~ I saved you a little invitation! Come explore with me, Senpai! (⁄ ⁄•⁄ω⁄•⁄ ⁄)✨",
    "🌸 Uwahh~ there you are! I've kept a cozy spot just for you. Tap below and say hello! (*≧ω≦)",
    "💌 E-Ehehe… a tiny invite from me to you! Let's discover something fun together, okay? (｡♥‿♥｡)",
    "✨ Knock knock~! Your next little adventure is waiting. Come join me, Senpai! (づ｡◕‿‿◕｡)づ",
)

ALIVE_LINES = {
    "home": (
        "🌸 E-Ehehe, I've organized all the spicy and exclusive media albums for you! What would you like to explore first, Senpai? (*≧ω≦)",
        "🌸 Uwahh~ you're back! I've tidied your shelves and saved your place. Where shall we wander today, Senpai? (｡♥‿♥｡)",
        "🎀 Ehehe, I was just getting everything ready for you! Your Vault and bookmarks are right here, Senpai~! (⁄ ⁄•⁄ω⁄•⁄ ⁄)",
        "✨ There you are, Senpai! Let's find your next favorite drop together. I'll handle the shelves; you pick the adventure! (*≧ω≦)",
    ),
    "referral": (
        "🌸 Keep inviting, Senpai~! (*≧ω≦)",
        "🎀 Ehehe~ your little circle is growing! Tap below to invite another friend, Senpai!",
        "💌 Another friend, another happy hello! I've tucked your reward safely into your balance~",
        "✨ Yatta~ teamwork looks cute on you! Shall we send one more invitation? (｡♥‿♥｡)",
    ),
    "delivery": (
        "🌸 Uwahh, don't worry! This collection is permanently saved in your Vault — tap Re-Stream anytime to watch again for free!",
        "🎀 All tucked into your Vault, Senpai! Re-Stream is free whenever you return. I'll keep your place~!",
        "✨ Delivery complete~! Your Vault remembers this collection, so you can Re-Stream for free anytime!",
        "🌸 Ehehe, your collection is safe in the Vault! Come back for a free Re-Stream whenever you like, Senpai~",
    ),
    "checkin": (
        "🌸 There you go, Senpai~! A tiny reward for showing up today!",
        "🎀 Ehehe, your daily treat is tucked into your balance!",
        "✨ Yatta~ another day together! Let's keep that streak glowing!",
        "💌 Your daily gift is ready! I'll be waiting for your next check-in, Senpai~",
    ),
}


def alive(scene):
    return cute(random.choice(ALIVE_LINES[scene]))


def share_text():
    return random.choice(SHARE_LINES)
