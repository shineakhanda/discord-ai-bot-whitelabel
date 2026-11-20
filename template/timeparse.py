"""Human time parsing in a server's timezone: '10m', '2h30m', '8pm', 'tomorrow 9am', 'friday 20:00', '2026-12-01 18:30'."""
import re, time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:                       # pragma: no cover
    ZoneInfo = None

UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
         "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400, "day": 86400,
         "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
DAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2, "thu": 3, "thur": 3,
        "thurs": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}

def tzinfo(name):
    if ZoneInfo:
        try:
            return ZoneInfo(name or "Asia/Dhaka")
        except Exception:
            pass
    return timezone(timedelta(hours=6))

def valid_tz(name):
    if not ZoneInfo:
        return name in ("Asia/Dhaka", "UTC")
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False

def duration(s):
    """'10m', '2h30m', '45' (minutes), '1 day' -> seconds or None."""
    s = s.strip().lower().removeprefix("in ").strip()
    if re.fullmatch(r"\d+", s):
        return int(s) * 60
    total, found = 0.0, False
    for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", s):
        if u in UNITS:
            total += float(n) * UNITS[u]; found = True
    rest = re.sub(r"(\d+(?:\.\d+)?)\s*([a-z]+)|and|,|\s", "", s)
    return int(total) if found and not rest else None

def _clock(s):
    """'8pm', '8:30 pm', '20:00', '9' -> (h, m) or None."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s.strip())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59 or (not ap and not m.group(2) and len(m.group(1)) <= 2 and h > 23):
        return None
    return h, mi

def parse_when(s, tz="Asia/Dhaka"):
    """Epoch seconds for a human time string, or None."""
    if not s:
        return None
    s = s.strip().lower()
    if s in ("now", "asap", "right now"):
        return time.time() + 5
    d = duration(s)
    if d:
        return time.time() + d
    z = tzinfo(tz)
    now = datetime.now(z)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ t]+(.+))?", s)
    if m:
        hm = _clock(m.group(4)) if m.group(4) else (9, 0)
        if not hm:
            return None
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), *hm, tzinfo=z).timestamp()
        except ValueError:
            return None
    m = re.fullmatch(r"(?:(today|tonight|tomorrow|tmrw|tmr)|([a-z]+))?\s*(?:at\s+)?(.*)", s)
    word, day, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
    if day and day not in DAYS:
        rest = (day + " " + rest).strip(); day = None
    hm = _clock(rest) if rest else ((20, 0) if word == "tonight" else (9, 0) if (word or day) else None)
    if not hm:
        return None
    target = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if word in ("tomorrow", "tmrw", "tmr"):
        target += timedelta(days=1)
    elif day:
        ahead = (DAYS[day] - now.weekday()) % 7
        target += timedelta(days=ahead)
        if target <= now:
            target += timedelta(days=7)
    elif target <= now:
        target += timedelta(days=1)
    return target.timestamp()

def parse_repeat(s):
    """'none' | 'daily' | 'weekly' | 'hourly' | 'every 6h' -> seconds (0 = once), None if invalid."""
    s = (s or "none").strip().lower()
    if s in ("", "none", "no", "once", "0"):
        return 0
    fixed = {"hourly": 3600, "daily": 86400, "every day": 86400, "weekly": 604800, "every week": 604800}
    if s in fixed:
        return fixed[s]
    d = duration(s.removeprefix("every ").strip())
    return d if d and d >= 3600 else None

def fmt_repeat(sec):
    if not sec:
        return "once"
    if sec % 604800 == 0:
        return "weekly" if sec == 604800 else f"every {sec // 604800}w"
    if sec % 86400 == 0:
        return "daily" if sec == 86400 else f"every {sec // 86400}d"
    return f"every {sec // 3600}h"
