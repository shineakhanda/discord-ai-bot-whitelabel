"""Brain: make the bot use tools (real data) instead of guessing.

1. auto_tools  — code decides and runs obvious tools before the AI answers:
                 maths, reminders, "remember …", personal facts, fresh-news web search.
2. plan        — for real questions, a short JSON "which tools do I need?" step; only safe read-only tools run.
3. unverified  — after the answer: every price/amount must come from data the bot actually has
                 (or be a simple calculation of it). Otherwise the answer is redone.
"""
import re, json

import ai, web
from store import log

# ---------------------------------------------------------------- 1. code-chosen tools
MATH_RE = re.compile(r"(?<![\w#:/.-])(\(?\d[\d,]*(?:\.\d+)?\s*%?\s*(?:(?:[-+*/x×÷^]|of)\s*\(?\s*\d[\d,]*(?:\.\d+)?\s*%?\s*\)?\s*)+)",
                     re.I)
NOT_MATH = re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$|^\d{6,}$")      # dates, ids, phone numbers

FRESH_RE = re.compile(
    r"\b(news|khobor|খবর|weather|abohawa|আবহাওয়া|forecast|live score|score of|who won|ke jit|result of|latest|"
    r"newest|today'?s|ajker|aj ki hoise|ekhon ki hocche|right now|this week|exchange rate|dollar rate|"
    r"usd to bdt|gold price|sonar dam|bitcoin|btc|eth\b|crypto price|stock price|share price|release date|"
    r"released|launch(ed)? date|new version|update ki|trending|viral|election|breaking)\b", re.I)

LEARN_RE = re.compile(r"\b(scan|crawl|learn|study|index|save|remember|mone rakh\w*|rekhe dao|shikh\w*|porho|pore nao)\b"
                      r".{0,60}\b(site|website|store|shop|page|pages|link|domain|data)\b|"
                      r"\b(site|website|store|shop)\b.{0,60}\b(scan|crawl|learn|save|remember|mone rakh\w*)\b|"
                      r"\b(scan|crawl|learn|index)\s+(https?://|[\w-]+\.[a-z]{2,})", re.I | re.S)
PRODUCT_RE = re.compile(r"\b(vps|rdp|vds|dedicated|bare ?metal|hosting|domain|minecraft|mc|plan|package|xeon|epyc|"
                        r"ryzen|intel|amd|reseller|wordpress|server|order|ticket)\b", re.I)
BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

REMEMBER_RE = re.compile(r"^\s*(?:please\s+)?(?:remember|mone rakh\w*|মনে রাখ\w*|note koro|save koro)\s*(?:that|je|:)?\s*"
                         r"(.{4,200})$", re.I | re.S)
FACT_RE = re.compile(
    r"\b(my name is|call me|amar nam|আমার নাম|amake .{1,20} (?:bolo|dako|ডাকো)|i(?:'m| am) from|"
    r"i live in|i work (?:as|at)|my (?:server|vps|plan|game|ign|username) is|amar (?:server|vps|plan|"
    r"game|ign) (?:holo|ache)|my birthday|i(?:'m| am) a (?:developer|student|gamer|designer|youtuber|streamer))\b"
    r"[^.?!\n]{1,120}", re.I)
REMIND_RE = re.compile(r"\bremind me\s+(in\s+\d+\s*\w+|at\s+[\w:.]+(?:\s*[ap]m)?|tomorrow(?:\s+(?:at\s+)?[\w:]+(?:\s*[ap]m)?)?|"
                       r"on\s+\w+(?:\s+[\w:]+)?)\s+(?:to\s+|about\s+|that\s+)?(.{3,200})", re.I)


def _math_exprs(text):
    out = []
    for m in MATH_RE.finditer(text or ""):
        e = m.group(1).strip().rstrip("+-*/x×÷^ ")
        if NOT_MATH.match(e) or not re.search(r"[-+*/x×÷^%]|of", e) or len(re.findall(r"\d+", e)) < 2:
            continue
        if re.fullmatch(r"\d+\s*-\s*\d+", e) and len(e) <= 5:          # "3-4" ranges, not maths
            continue
        e = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", e, flags=re.I)
        e = re.sub(r"([\d.,]+)\s*([+-])\s*([\d.]+)\s*%", r"\1*(1\g<2>\3/100)", e)   # 2550 + 3%  -> 2550*1.03
        out.append(e)
    return out[:3]


async def auto_tools(ctx, text, names, act, show, info_loaded=False):
    """Run the tools the message obviously needs. Returns [(tool, result_text)]."""
    done, t = [], (text or "").strip()
    if not t:
        return done
    if "calc" in names:
        for e in _math_exprs(t):
            try:
                done.append(("calc", f"{e} = {web.calc(e)}"))
            except Exception:
                pass
    if "remind" in names:
        m = REMIND_RE.search(t)
        if m:
            when = re.sub(r"^(in|at|on)\s+", "", m.group(1).strip(), flags=re.I)
            res = await act("remind", f"{when} | {m.group(2).strip()}")
            done.append(("remind", res.text))
    if "remember" in names:
        m = REMEMBER_RE.match(re.sub(r"^\W*\w+\W+", "", t) if t.lower().startswith(("hey", "ok", "bro")) else t)
        facts = [m.group(1).strip()] if m else [f.group(0).strip() for f in FACT_RE.finditer(t)][:2]
        for f in facts:
            res = await act("remember", f)
            done.append(("remember", res.text))
    if "learn_site" in names and LEARN_RE.search(t):
        m = re.search(r"https?://\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|xyz|io|dev|bd|shop|store|app|co|me|gg|in)\b\S*", t)
        if m:
            res = await act("learn_site", m.group().rstrip(").,!?>"))
            done.append(("learn_site", res.text))
            return done
    if "search" in names and FRESH_RE.search(t) and len(t) > 8 and not (info_loaded and PRODUCT_RE.search(t)):
        q = await search_query(t)
        await show("search", q)
        res = await act("search", q)
        done.append(("search", res.text))
    return done


async def search_query(text):
    """Turn a chat message (often Banglish) into a short English web search query."""
    plain = re.sub(r"^(hey|yo|bro|ok)\W+", "", text, flags=re.I)[:200]
    try:
        q, _ = await ai.ask([{"role": "system", "content":
                              "Write ONE short English web search query (max 10 words) for what this Discord message "
                              "wants to know. Ignore the bot's name. Add the place or 'today' if it matters. "
                              "Reply with the query only."},
                             {"role": "user", "content": plain}], max_tokens=30, temperature=0)
        q = (q or "").strip().strip('"').splitlines()[0][:150] if q else ""
        return q if len(q) > 3 else plain
    except Exception:
        return plain


# ---------------------------------------------------------------- 2. tool plan
SAFE_PLAN = {"search", "read", "channel", "history", "calc", "skill", "server_status", "panel_list", "panel_logs",
             "leaderboard", "mod_warnings", "mod_cases", "schedule_list", "ssh_hosts", "panel_nodes", "panel_users",
             "panel_server_find", "panel_files", "panel_read"}
CHAT_RE = re.compile(r"^\W*(hi+|hello+|hey+|yo+|salam|assalamu\S*|kemon (acho|achen|aso)|ki obostha|thanks?|thank you|"
                     r"tnx|ty|ok+|okay|hmm+|lol|haha+|good (morning|night)|gm|gn|bye)\W*$", re.I)


def worth_planning(text, already):
    t = (text or "").strip()
    if already or len(t) < 18 or CHAT_RE.match(t):
        return False
    return "?" in t or len(t) > 40 or bool(re.search(
        r"\b(how|what|why|which|when|where|who|koto|kivabe|keno|kothay|ki ki|kon|check|find|show|tell|explain|"
        r"compare|list|status|logs?|fix|error|problem)\b", t, re.I))


async def plan(ctx, text, names, context_hint=""):
    """Ask the model, briefly, which read-only tools it needs. Returns [(name, input)]."""
    usable = sorted(n for n in names if n in SAFE_PLAN)
    if not usable:
        return []
    from agent import TOOLS
    lines = "\n".join(f"- {TOOLS[n].usage} — {TOOLS[n].desc}" for n in usable if n in TOOLS)
    prompt = ("You decide which tools to run BEFORE answering a Discord message. Prefer real data over memory: use a "
              "tool whenever the answer depends on current facts, this server's channels, a web page, earlier chat, a "
              "game/VPS server's state, or maths. Use none for chit-chat, opinions, or things you surely know.\n"
              f"Tools:\n{lines}\n\n{context_hint}"
              'Reply with JSON only: {"tools": [{"name": "<tool>", "input": "<input>"}]} — at most 3, or {"tools": []}.')
    try:
        raw, _ = await ai.ask([{"role": "system", "content": prompt}, {"role": "user", "content": text[:1500]}],
                              max_tokens=160, temperature=0)
    except Exception as e:
        log.info("planner skipped: %s", e)
        return []
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        items = json.loads(m.group()).get("tools", []) if m else []
    except Exception:
        return []
    out = []
    for it in items[:3]:
        if isinstance(it, dict) and it.get("name") in usable and str(it.get("input", "")).strip():
            out.append((it["name"], str(it["input"]).strip()[:300]))
    return out


# ---------------------------------------------------------------- 3. answer check: no invented amounts
MONEY_RE = re.compile(r"(?:৳|\$|€|£|₹|\btk\.?|\bbdt|\busd)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:৳|\$|"
                      r"\btk\b|\btaka\b|টাকা|\bbdt\b|\busd\b|/mo\b|/month\b|per month)", re.I)
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _num(s):
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def unverified(reply, source_text):
    """Amounts in the reply that aren't in the data and aren't a simple calculation of it
    (× months, ± a percentage from the data, or two amounts added)."""
    reply, source_text = (reply or "").translate(BN_DIGITS), (source_text or "").translate(BN_DIGITS)
    amounts = {_num(a or b) for a, b in MONEY_RE.findall(reply or "")} - {None}
    if not amounts:
        return []
    src = {n for n in (_num(x) for x in NUM_RE.findall(source_text or "")) if n is not None}
    pct = {n for n in (_num(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", source_text or "")) if n}
    big = [n for n in src if n >= 1][:400]
    bad = []
    for a in amounts:
        if a in src or a <= 1:
            continue
        ks = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 18, 24, 36)
        ok = any(abs(a - x * k) < 0.6 for x in big for k in ks)
        ok = ok or any(abs(a - x * k * f) < 0.6 for x in big for k in ks for p in pct
                       for f in (1 + p / 100, p / 100, 1 - p / 100))
        ok = ok or any(abs(a - (x + y)) < 0.6 for i, x in enumerate(big[:150]) for y in big[i:150])
        if not ok:
            bad.append(a)
    return [f"{b:,.2f}".rstrip("0").rstrip(".") for b in bad][:6]


GROUND_NOTE = ("CHECK FAILED: these amounts in your answer are not in your data and are not a simple calculation "
               "of it: {bad}. Never invent prices or amounts. If it's a calculation, run it: `TOOL: calc | <expression>`. "
               "If the data is missing, read it (`TOOL: channel | <name>` / `TOOL: search | <query>`) or remove the "
               "amount and say where to check. Then answer again.")
