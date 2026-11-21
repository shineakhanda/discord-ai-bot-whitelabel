"""Passive answers: questions the code can answer exactly, instantly, with no AI model.

Only fires when it is sure (one clear plan/product, a pure sum, weather, time, a reminder). Anything else -> AI.
"""
import re, sys, datetime

import web, store

PRICE_Q = re.compile(r"\b(price|prices|pricing|dam|daam|koto|cost|costs|rate|how much|kmn|কত|দাম)\b|\?$", re.I)
NEEDS_AI = re.compile(r"\b(month|months|mash|mas|year|total|discount|charge|compare|vs|better|best|which|kon ta|"
                      r"recommend|suggest|why|keno|how to|kivabe|plus|\+|%|x\s*\d|\d\s*x|×)\b|[+%×]", re.I)
PRICE_RE = re.compile(r"(?:[$৳€£₹]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:tk|bdt|usd|taka|৳|\$)(?![a-z]))"
                      r"(?:\s*/\s*(?:mo|month|yr|year))?", re.I)
CODE_RE = re.compile(r"\b([A-Za-z][A-Za-z]{1,14})[-\s]?(\d{1,4}[A-Za-z]?)\b")
TIME_Q = re.compile(r"\b(what time|time koto|koyta baje|ko[iy]ta baje|কয়টা বাজে|what('s| is) the date|today'?s date|"
                    r"ajke koto tarikh|aj koto tarikh|ajke ki bar|aj ki bar|what day is it)\b", re.I)
WEATHER_Q = re.compile(r"\b(weather|abohawa|আবহাওয়া|temperature|tapmatra)\b", re.I)
PURE_MATH = re.compile(r"^[\d\s.,+\-*/x×÷()%^]+$")


def _fmt_block(seg):
    seg = re.sub(r"[*_`~]+", "", seg)
    seg = re.sub(r"\s*\n+\s*", " · ", seg.strip())
    seg = re.sub(r"(\s*·\s*){2,}", " · ", seg)
    return seg.strip(" ·-")


def plan_answer(info, text):
    """'INTEL-32 er dam koto?' -> the exact INTEL-32 block from the info channels."""
    if not info or not info.startswith("## "):
        return None
    codes = {(a.upper(), b.upper()) for a, b in CODE_RE.findall(text)
             if a.lower() not in {"the", "er", "ki", "koto", "dam", "plan", "for", "and", "in", "on", "at"}}
    if len(codes) != 1:
        return None
    a, b = codes.pop()
    channel, hit = "", None
    for block in info.split("\n## "):
        head, _, body = block.lstrip("# ").partition("\n")
        m = re.search(rf"\b{re.escape(a)}[-\s]?{re.escape(b)}\b", body, re.I)
        if not m:
            continue
        tail = body[m.start(): m.start() + 260]
        pm = PRICE_RE.search(tail)
        if not pm:
            continue
        hit, channel = tail[: pm.end()], head.strip()
        break
    if not hit:
        return None
    name, _, rest = _fmt_block(hit).partition(" ")
    return f"**{name}** {rest}".strip(), channel


def product_answer(gid, text):
    sb = sys.modules.get("features.sitebrain")
    if not sb:
        return None
    items = sb.item_matches(gid, text, limit=4)
    if not items:
        return None
    words = {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}
    strong = [i for i in items if set(re.findall(r"[a-z0-9]{3,}", i[0].lower())) <= words | {"the", "and"}]
    if not strong or len(strong) > 3:
        return None
    lines = [f"• **{n}** — **{p}**" + (f" · {e}" if e else "") + f"\n  <{u}>" for n, p, u, e in strong]
    return "\n".join(lines)


INSTRUCTION = re.compile(r"^(you|u)\b|\byou (are|must|should|know|can)\b|\bbehave\b|\bpolite|\bact as\b|\bnever say\b|\balways (say|reply|answer)\b|\bsystem prompt\b", re.I)
RULE_TOPIC = re.compile(r"charge|fee|discount|% ?off|vat|tax|ticket|maintenance|rate", re.I)
PCT_FEE = re.compile(r"\d+(\.\d+)?\s*%.*(charge|fee)|(charge|fee).*\d+(\.\d+)?\s*%", re.I)


def _rules(gid):
    """Customer-facing price rules (charges, discounts, how to buy) saved by admins.
    Staff instructions are never shown, and when several lines set a % fee the newest one wins."""
    if not gid:
        return ""
    raw = (store.guild(gid).get("knowledge") or "").replace("\r", "")
    parts = []
    for line in raw.splitlines():
        for sent in re.split(r"(?<=[.!?])\s+", line.strip(" -•\t")):
            sent = sent.strip(" -•.")
            if sent and RULE_TOPIC.search(sent) and not INSTRUCTION.search(sent):
                parts.append(sent)
    fees = [p for p in parts if PCT_FEE.search(p)]
    if len(fees) > 1:  # newest fee rule replaces older ones
        parts = [p for p in parts if p not in fees[:-1]]
    seen, keep = set(), []
    for p in parts:
        k = p.lower()
        if k not in seen:
            seen.add(k); keep.append(p)
    return "".join(f"\n• {l[:160]}" for l in keep[:3])


def _tz(gid):
    import zoneinfo
    name = (store.guild(gid).get("timezone") if gid else None) or "Asia/Dhaka"
    try:
        return zoneinfo.ZoneInfo(name), name
    except Exception:
        return zoneinfo.ZoneInfo("Asia/Dhaka"), "Asia/Dhaka"


async def answer(ctx, text, info, run_tool):
    """Returns (reply, step_label) or None when the AI should handle it."""
    t = (text or "").strip()
    if not t or len(t) > 160:
        return None
    # pure maths
    expr = t.rstrip("?= ").strip()
    if PURE_MATH.match(expr) and re.search(r"\d\s*[-+*/x×÷^%]\s*\(?\d", expr):
        try:
            e = re.sub(r"(?<=\d)\s*[x×]\s*(?=\d)", "*", expr).replace("÷", "/")
            e = re.sub(r"([\d.,]+)\s*([+-])\s*([\d.]+)\s*%", r"\1*(1\g<2>\3/100)", e)
            return f"**{expr} = {web.calc(e)}**", "🧮 calculated"
        except Exception:
            return None
    # time / date
    if TIME_Q.search(t):
        tz, name = _tz(ctx.gid)
        now = datetime.datetime.now(tz)
        return (f"🕒 **{now.strftime('%I:%M %p').lstrip('0')}** · {now.strftime('%A, %d %B %Y')}"
                f"\n-# {name}"), "🕒 clock"
    # weather
    import agent
    if WEATHER_Q.search(t) and not NEEDS_AI.search(t) and agent.focus_allows("weather"):
        place = re.sub(r"\b(weather|abohawa|temperature|tapmatra|kemon|ki|er|in|of|at|the|now|today|ajke|aj|koto|"
                       r"what|is|how|bolo|tell|me|hobe)\b|[?!.,]|আবহাওয়া", " ", t, flags=re.I)
        place = " ".join(place.split()) or "Dhaka"
        res = await run_tool(ctx, "weather", place)
        if res.text.startswith("Weather for"):
            return "☔ " + res.text.replace("\n", "\n• ", 1).replace("\n", "\n• ").replace("• • ", "• "), "☔ weather"
        return None
    # prices of one plan / product
    if PRICE_Q.search(t) and not NEEDS_AI.search(t):
        hit = plan_answer(info, t)
        if hit:
            line, channel = hit
            return f"{line}{_rules(ctx.gid)}\n-# from {channel} · to order, open a ticket", "⚡ instant answer"
        if ctx.gid:
            block = product_answer(ctx.gid, t)
            if block:
                return block + _rules(ctx.gid) + "\n-# from the store website", "⚡ instant answer"
    return None
