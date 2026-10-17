"""Cute persona strings for Bot 3 — the bot talks like it's alive.

Every user-facing card pulls its wording from here so the tone stays
consistent and editable in one place. BOT_NAME is configurable.
"""
import config

SEP = "━━━━━━━━━━━━━━━━━━━━"


def _n() -> str:
    return config.BOT_NAME


def home(balance: int, streak: int, unlocked: int, saved: int) -> str:
    return (
        f"🎀 Uwahh~! Welcome to {_n()}! (⁄ ⁄•⁄ω⁄•⁄ ⁄)✨\n{SEP}\n"
        f"👛 Token Balance: {balance} Tokens\n"
        f"🔥 Daily Streak: {streak} Days\n"
        f"🔓 Unlocked Vault: {unlocked} Drops\n"
        f"⭐ Saved Bookmarks: {saved} Posts\n{SEP}\n"
        f"🌸 E-Ehehe, I've organized all the spicy and exclusive media albums "
        f"for you! What would you like to explore first, Senpai? (*≧ω≦)")


def categories(balance: int) -> str:
    return (
        f"🎀 Explore Categories, Senpai~! (｡♥‿♥｡)✨\n{SEP}\n"
        f"👛 Balance: {balance} Tokens\n\n"
        f"Filter my collection shelves by category — every drop costs "
        f"{config.COLLECTION_PRICE} tokens!\n{SEP}\n"
        f"🌸 Uwahh, tap any shelf below and I'll fetch the perfect album "
        f"for you!")


def feed_empty() -> str:
    return ("🎀 E-Eh? This shelf is still empty, Senpai... (´；ω；`)\n"
            "New drops are being prepared — check back soon, okay?")


def feed_caption(title: str, files: int, views: int, unlocks: int,
                 price: int, balance: int, unlocked: bool) -> str:
    state = ("✅ Already in your Vault — Re-Stream is FREE!"
             if unlocked else f"🔒 Unlock for {price} Tokens")
    return (
        f"🎀 New Drop ོ࿐\n{SEP}\n{title}\n{SEP}\n"
        f"📦 {files} media files\n"
        f"👁 Views: {views} • ❤️ Unlocks: {unlocks}\n"
        f"🪙 Cost: {price} tokens • 👛 Balance: {balance}\n{SEP}\n"
        f"{state}")


def insufficient(price: int, balance: int) -> str:
    return (f"🎀 A-Ahh, not enough tokens, Senpai! (；￣Д￣)\n{SEP}\n"
            f"🪙 This drop costs {price} tokens but you only have "
            f"{balance}.\n\n🌸 Tap Earn Tokens and I'll fix you right up!")


def stream_complete(title: str, delivered: int, price_paid: int) -> str:
    mins = config.AUTO_DELETE_AFTER_SECONDS // 60
    return (
        f"🎀 Stream Complete, Senpai~! (*≧ω≦)✨\n{SEP}\n"
        f"✨ COLLECTION BY {_n()} ✨\n\n🎬 {title}\n{SEP}\n"
        f"📦 Delivered {delivered} items in album format • "
        f"Auto-deletes in {mins} min ⏳\n{SEP}\n"
        f"🌸 Uwahh, don't worry! This collection is permanently saved in "
        f"your Vault — tap Re-Stream anytime to watch again for free!")


def delivery_failed_refund(price: int, balance: int) -> str:
    return (f"🎀 G-Gomen, Senpai! The delivery failed... (╥﹏╥)\n{SEP}\n"
            f"💸 I refunded your {price} tokens right away!\n"
            f"👛 Balance: {balance} Tokens\n\n"
            f"🌸 Please tap Re-Stream to try again, okay?")


def help_card() -> str:
    return (
        f"🎀 Cute Assistant's Guide & Manual (⁄ ⁄•⁄ω⁄•⁄ ⁄)✨\n{SEP}\n"
        f"U-Uwahh, don't worry if you get lost, Senpai! I'll explain how "
        f"everything works step by step! (*≧ω≦)🌸\n\n"
        f"1. Instant Media Stream 🎬:\n"
        f"• Tap Start Watching to browse drops!\n"
        f"• Next / Previous for smooth browsing.\n"
        f"• Categories filters by shelf!\n\n"
        f"2. One-Tap Unlocking & Albums 📦:\n"
        f"• Unlock costs {config.COLLECTION_PRICE} tokens — delivered as "
        f"clean albums!\n"
        f"• Unlocked drops stay in your Vault forever — Re-Stream FREE!\n"
        f"• Delivered media auto-cleans after "
        f"{config.AUTO_DELETE_AFTER_SECONDS // 60} min so your chat stays "
        f"neat & tidy.\n\n"
        f"3. Free Token Rewards 👛:\n"
        f"• /checkin daily for +{config.DAILY_CHECKIN_REWARD} tokens & "
        f"streak bonuses!\n"
        f"• Earn Tokens tasks renew every 24 hours!\n"
        f"• Refer Friends: you +{config.REFERRAL_INVITER_BONUS}, friend "
        f"+{config.REFERRAL_FRIEND_BONUS}! 🎁\n{SEP}\n"
        f"🌸 If you ever need me, just tap Main Menu below, okay? Ehehe~")


def profile(user_id: int, balance: int, streak: int, unlocked: int,
            saved: int, earned: int, spent: int, tasks: int,
            referrals: int) -> str:
    return (
        f"🎀 Senpai's Profile & Vault (⁄ ⁄•⁄ω⁄•⁄ ⁄)✨\n{SEP}\n"
        f"🆔 Account ID: {user_id}\n"
        f"👛 Balance: {balance} Tokens\n"
        f"🔥 Streak: {streak} Days\n\n"
        f"Your Personal Collection:\n"
        f"• Unlocked Vault: {unlocked} Collections (Permanent Free "
        f"Re-Stream)\n"
        f"• Saved Bookmarks: {saved} Posts\n\n"
        f"Economy Telemetry:\n"
        f"• Total Earned: {earned} Tokens\n"
        f"• Total Spent: {spent} Tokens\n"
        f"• Tasks Solved: {tasks}\n"
        f"• Friends Referred: {referrals}\n{SEP}\n"
        f"🌸 All your unlocked drops are permanently preserved in your "
        f"Vault! Re-stream them anytime for free! (*≧ω≦)")


def earn_home(balance: int) -> str:
    return (
        f"🎀 Earn Free Tokens, Senpai~! (*≧ω≦)✨\n{SEP}\n"
        f"👛 Your Balance: {balance} Tokens\n\n"
        f"How to earn free tokens:\n"
        f"1. Tap an available task button below (no cost!).\n"
        f"2. Complete the quick verification sponsor link.\n"
        f"3. You'll be redirected back with tokens instantly added!\n\n"
        f"🌸 Ehehe, each task renews every 24 hours so you can earn more "
        f"every single day!")


def task_unavailable() -> str:
    return ("🎀 N-No tasks are open right now, Senpai... (｡•́︿•̀｡)\n"
            "The owner hasn't configured the sponsor links yet — come back "
            "later, okay?")


def task_cooldown(hours: int, minutes: int) -> str:
    return (f"⏳ Task Cooling Down!\n\nCome back in {hours}h {minutes}m for "
            f"this task again.\n\n💡 Meanwhile, try /checkin or invite "
            f"friends!")


def checkin_done(base: int, bonus: int, balance: int, streak: int,
                 next_bonus_in: int) -> str:
    total = base + bonus
    bonus_line = (f"🎁 Base Reward: +{base} tokens\n"
                  f"🔥 Streak Bonus: +{bonus} token\n" if bonus else
                  f"🎁 Base Reward: +{base} tokens\n")
    return (
        f"✅ Daily Check-In Complete!\n{SEP}\n{bonus_line}"
        f"💰 Total Earned: +{total} tokens\n"
        f"🪙 Balance: {balance} tokens\n\n"
        f"🔥 Streak: {streak} days\n"
        f"📈 Next streak bonus in {next_bonus_in} day(s) "
        f"(+{config.STREAK_BONUS} token)")


def checkin_already(hours: int, minutes: int) -> str:
    return (f"⏳ Already Checked In!\n\nCome back in {hours}h {minutes}m "
            f"for your next reward.\n\n💡 Meanwhile, earn more tokens via "
            f"shortener tasks!")


def referral(link: str, friends: int, earned: int) -> str:
    return (
        f"🎀 INVITE FRIENDS & SHARE\n{SEP}\n"
        f"Share your personal link to invite friends:\n{link}\n\n"
        f"Rewards:\n"
        f"• You get: +{config.REFERRAL_INVITER_BONUS} Free Tokens per "
        f"invited friend\n"
        f"• Friend gets: +{config.REFERRAL_FRIEND_BONUS} Bonus Tokens on "
        f"joining!\n\n"
        f"Your Referrals: {friends} Friends (Earned: +{earned} Tokens)\n"
        f"{SEP}")


def referral_notify(friend_name: str, bonus: int, balance: int) -> str:
    return (f"🎉 Yatta! Your friend {friend_name} joined and passed the "
            f"channel check!\n\n🎁 Referral Reward: +{bonus} tokens\n"
            f"🪙 Updated Balance: {balance} Tokens\n\n"
            f"🌸 Keep inviting, Senpai~! (*≧ω≦)")


def join_required(channels: list) -> str:
    lines = "\n".join(f"• {c}" for c in channels) if channels else \
        "• (owner hasn't set channels yet)"
    return (f"🎀 W-Wait, Senpai! (｡•́︿•̀｡)\n{SEP}\n"
            f"You need to join our channel(s) before unlocking:\n{lines}\n\n"
            f"🌸 Join, then tap the button again — I'll be waiting!")
