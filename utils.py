"""Small shared helpers: flexible duration parsing + admin guard."""
import functools
import logging
import re
import time

import config
import db

log = logging.getLogger("utils")

# Human-friendly time units -> seconds
_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_duration(text: str):
    """Parse flexible durations into SECONDS.

    Accepts: '5min', '2hour', '12hour', '7day', '1day', '30m', '90s',
             'never'/'0' (-> 0 = no auto-delete), and v4.0 COMPOUND forms
             like '1h 2m', '2h30m', '1day 12hour' (units simply add up).
    A bare number is interpreted as MINUTES ('30' -> 1800).
    Returns None when the string cannot be understood.
    """
    if text is None:
        return None
    t = str(text).strip().lower()
    if not t:
        return None
    if t in ("0", "off", "never", "none"):
        return 0
    if t.isdigit():
        return int(t) * 60  # bare number = minutes
    # v4.0: compound durations — one or more <number><unit> segments.
    total, matched = 0, 0
    for value, unit in re.findall(r"(\d+)\s*([a-z]+)", t):
        if unit not in _UNITS:
            return None
        total += int(value) * _UNITS[unit]
        matched += 1
    # Reject strings that only PARTIALLY match ('1h banana', 'abc 2m').
    if matched and "".join(re.findall(r"\d+|[a-z]+", t)) == "".join(
            re.findall(r"\d+|[a-z]+", " ".join(
                f"{v}{u}" for v, u in re.findall(r"(\d+)\s*([a-z]+)", t)))):
        return total
    return None


def human_duration(seconds: int) -> str:
    """Turn a seconds count into a readable string like '2 hours 5 minutes'.
    v4.0: returns 'never' (not the longer kept-forever phrasing) so broadcast
    confirmations read naturally ('Deleted after: never')."""
    if not seconds or seconds <= 0:
        return "never"
    parts = []
    for label, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{n} {label}{'s' if n != 1 else ''}")
    return " ".join(parts[:2])


def now() -> float:
    return time.time()


async def is_admin(user_id: int) -> bool:
    """Admin if listed in ADMIN_IDS env OR promoted via /addadmin."""
    if user_id in config.ADMIN_IDS:
        return True
    try:
        return await db.is_db_admin(user_id)
    except Exception:  # never let a db hiccup crash a handler
        return False


def admin_only(func):
    """Decorator: silently reject non-admins on admin commands."""
    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        if user and await is_admin(user.id):
            return await func(update, context, *args, **kwargs)
        message = update.effective_message
        if message:
            await message.reply_text(
                "⛔ This command can be used only by admins.")
        return None

    return wrapper


def format_ram_report() -> str:
    """v4.7 /checkram: RAM usage of the ONE process that hosts BOTH bots.

    Bot 1 and Bot 2 are two python-telegram-bot Applications inside a single
    Python process, so the OS only tracks ONE footprint — the process total
    IS what Render counts against the plan. The per-bot split below is an
    in-process estimate (dispatcher thread stacks), not separate processes.
    Prefers psutil; falls back to /proc/self so it also works without it."""
    vm = None
    peak = None
    try:
        import psutil
        p = psutil.Process()
        mi = p.memory_info()
        total = getattr(mi, "rss", 0) or 0
        vms = getattr(mi, "vms", 0) or 0
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        peak = int(line.split()[1]) * 1024
                        break
        except Exception:
            peak = None
        vm = psutil.virtual_memory()
    except Exception:
        def _read_kb(field):
            try:
                with open("/proc/self/status") as fh:
                    for line in fh:
                        if line.startswith(field):
                            return int(line.split()[1]) * 1024
            except Exception:
                return None
            return None
        total = _read_kb("VmRSS:") or 0
        vms = _read_kb("VmSize:") or 0
        peak = _read_kb("VmHWM:")

    def mb(n):
        return f"{n / 1024 / 1024:.1f} MB" if n else "?"

    # ~3 dispatcher/handler threads per bot, ~2 MB reserved stack each.
    bot_share = 3 * 2 * 1024 * 1024
    shared = max(total - 2 * bot_share, 0)
    lines = [
        "🖥 <b>RAM usage</b>",
        "",
        f"<b>Both bots (whole process):</b> {mb(total)}"
        + (f" — peak {mb(peak)}" if peak else ""),
        f"<b>Bot 1 (gate):</b> ~{mb(shared / 2 + bot_share)}",
        f"<b>Bot 2 (delivery):</b> ~{mb(shared / 2 + bot_share)}",
    ]
    if vm is not None:
        lines += [
            "",
            f"Server RAM: {mb(vm.used)} / {mb(vm.total)} ({vm.percent:.0f}% used)",
            f"Free right now: {mb(vm.available)}",
        ]
    if vms:
        lines.append(f"Virtual address space: {mb(vms)}")
    lines += [
        "",
        "ℹ️ Both bots share ONE Python process (one Render service), so the "
        "per-bot split is an estimate — Telegram/Render only see the total.",
    ]
    return "\n".join(lines)


def format_ram_report() -> str:
    """v4.7 /checkram: RAM usage of the ONE process that hosts BOTH bots.

    Bot 1 and Bot 2 are two python-telegram-bot Applications inside a single
    Python process, so the OS only tracks ONE footprint — the process total
    IS what Render counts against the plan. The per-bot split below is an
    in-process estimate (dispatcher thread stacks), not separate processes.
    Prefers psutil; falls back to /proc/self so it also works without it."""
    vm = None
    peak = None
    try:
        import psutil
        p = psutil.Process()
        mi = p.memory_info()
        total = getattr(mi, "rss", 0) or 0
        vms = getattr(mi, "vms", 0) or 0
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        peak = int(line.split()[1]) * 1024
                        break
        except Exception:
            peak = None
        vm = psutil.virtual_memory()
    except Exception:
        def _read_kb(field):
            try:
                with open("/proc/self/status") as fh:
                    for line in fh:
                        if line.startswith(field):
                            return int(line.split()[1]) * 1024
            except Exception:
                return None
            return None
        total = _read_kb("VmRSS:") or 0
        vms = _read_kb("VmSize:") or 0
        peak = _read_kb("VmHWM:")

    def mb(n):
        return f"{n / 1024 / 1024:.1f} MB" if n else "?"

    # ~3 dispatcher/handler threads per bot, ~2 MB reserved stack each.
    bot_share = 3 * 2 * 1024 * 1024
    shared = max(total - 2 * bot_share, 0)
    lines = [
        "🖥 <b>RAM usage</b>",
        "",
        f"<b>Both bots (whole process):</b> {mb(total)}"
        + (f" — peak {mb(peak)}" if peak else ""),
        f"<b>Bot 1 (gate):</b> ~{mb(shared / 2 + bot_share)}",
        f"<b>Bot 2 (delivery):</b> ~{mb(shared / 2 + bot_share)}",
    ]
    if vm is not None:
        lines += [
            "",
            f"Server RAM: {mb(vm.used)} / {mb(vm.total)} ({vm.percent:.0f}% used)",
            f"Free right now: {mb(vm.available)}",
        ]
    if vms:
        lines.append(f"Virtual address space: {mb(vms)}")
    lines += [
        "",
        "ℹ️ Both bots share ONE Python process (one Render service), so the "
        "per-bot split is an estimate — Telegram/Render only see the total.",
    ]
    return "\n".join(lines)
