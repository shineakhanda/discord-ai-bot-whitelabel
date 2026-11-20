"""Collect data from websites (including JavaScript/React sites, rendered by Lightpanda) into a neat table and a
CSV file, and watch pages for changes (price, stock, status) and post in a channel when they change."""
import csv, io, json, re, time, hashlib, asyncio
from typing import Optional

import discord
from discord import app_commands

import store, web
from store import db, log
from core import client, tree, embed, is_admin, safe_send, on_startup, feature
from agent import tool, Result, one_shot

FOOTER = "__BOT_NAME__ web data"
MAX_WATCHES = {"free": 5, "premium": 25}
MIN_EVERY = 15                                     # minutes

db.executescript("""
CREATE TABLE IF NOT EXISTS watches (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
                                    url TEXT, what TEXT, every INTEGER, last_value TEXT DEFAULT '',
                                    last_hash TEXT DEFAULT '', last_check REAL DEFAULT 0, by_id INTEGER,
                                    fails INTEGER DEFAULT 0, created REAL);
""")
db.commit()

EXTRACT = """You turn web page content into a clean data table.
The user wants: {what}
Rules:
- Use ONLY data that is in the page content. Never invent rows or values. If something is missing, leave it empty.
- Pick sensible column names (short, Title Case). Keep numbers/prices exactly as written (with currency).
- At most {max_rows} rows.
- Reply with ONLY JSON: {{"columns": ["..."], "rows": [["...", "..."]], "note": "one short line about the data"}}
- If the page has nothing matching, reply {{"columns": [], "rows": [], "note": "why"}}"""

async def extract(url, what, max_rows=200):
    final, title, text = await web.read_raw(url)
    if not text.strip():
        raise RuntimeError("the page had no readable content")
    raw = await one_shot(EXTRACT.format(what=what or "the main data on this page (tables, lists, products, prices)",
                                        max_rows=max_rows),
                         f"PAGE: {final}\nTITLE: {title}\n\n{text[:24000]}", max_tokens=3000, temperature=0.1,
                         smart=True)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError("couldn't turn that page into a table")
    data = json.loads(m.group(0))
    cols = [str(c)[:60] for c in data.get("columns") or []]
    rows = [[str(x)[:300] for x in (r + [""] * (len(cols) - len(r)))[:len(cols)]]
            for r in data.get("rows") or [] if isinstance(r, list)]
    return final, title, cols, rows[:max_rows], str(data.get("note", ""))[:200]

def to_csv(cols, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")       # Excel opens Bangla text correctly with the BOM

def preview(cols, rows, n=8, width=18):
    """Small monospace table for Discord."""
    if not cols:
        return ""
    cut = lambda s: (s if len(s) <= width else s[:width - 1] + "…")
    show = [[cut(c) for c in cols]] + [[cut(x) for x in r] for r in rows[:n]]
    ws = [max(len(r[i]) for r in show) for i in range(len(cols))]
    line = lambda r: " | ".join(x.ljust(ws[i]) for i, x in enumerate(r))
    out = [line(show[0]), "-+-".join("-" * w for w in ws)] + [line(r) for r in show[1:]]
    txt = "\n".join(out)
    while len(txt) > 1500 and len(out) > 3:
        out.pop()
        txt = "\n".join(out)
    return f"```\n{txt}\n```"

def _slug(url):
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://(www\.)?", "", url.lower()))[:40].strip("-") or "data"

# ---------- AI tool ----------
@tool("web_extract", "web_extract | <url> | <what to collect, e.g. 'all products with price and stock'>",
      "open a website (JavaScript/React sites too) and collect data into a table + CSV file",
      when=lambda c: feature(c.gid, "browse"), progress="🕸️ Collecting data from the page…", heavy=True)
async def t_extract(c, a):
    a = (a + ["", ""])[:2]
    if not a[0].strip():
        return Result("Usage: web_extract | <url> | <what to collect>")
    if c.gid and not store.quota(c.gid, c.user.id, "browse"):
        return Result("Today's browsing limit for this server is used up.")
    try:
        final, title, cols, rows, note = await extract(a[0].strip(), a[1].strip())
    except web.BlockedURL:
        return Result("That address isn't allowed (private or local network).")
    except Exception as e:
        return Result(f"Couldn't collect data: {e}")
    if not rows:
        return Result(f"No matching data found on {final}. {note}")
    f = discord.File(io.BytesIO(to_csv(cols, rows)), filename=f"{_slug(final)}.csv")
    c.after_send.append((f"📎 **{len(rows)} rows** from <{final}> — CSV opens in Excel / Google Sheets"
                         + (f"\n-# {note}" if note else ""), None, {"file": f}))
    sample = "; ".join(" / ".join(r) for r in rows[:15])
    return Result(f"Collected {len(rows)} rows, columns: {', '.join(cols)}. A CSV file is attached below your reply. "
                  f"Show a short preview using this table:\n{preview(cols, rows)}\nFirst rows: {sample[:1500]}",
                  f"🕸️ collected {len(rows)} rows")

# ---------- page watching ----------
NOISE = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b|\b\d+\s*(sec|second|min|minute|hour)s?\s+ago\b|©.*", re.I)

def _norm_lines(text):
    out = []
    for ln in text.splitlines():
        ln = NOISE.sub("", " ".join(ln.split()))
        if len(ln) > 3:
            out.append(ln)
    return out

async def check_value(url, what):
    """Returns (value_text, hash)."""
    final, title, text = await web.read_raw(url, limit=40000)
    if what:
        val = await one_shot("From the page content, report ONLY the requested value(s), short and exact "
                             "(e.g. '৳ 1,250 · In stock'). If not found, reply exactly NOT_FOUND.",
                             f"Requested: {what}\n\nPAGE: {final}\n{text[:16000]}", max_tokens=120, temperature=0)
        val = val.strip().splitlines()[0][:300] if val.strip() else "NOT_FOUND"
        return val, hashlib.sha1(val.lower().encode()).hexdigest()
    lines = _norm_lines(text)
    return "\n".join(lines)[:20000], hashlib.sha1("\n".join(lines).encode()).hexdigest()

def _diff(old, new, n=6):
    o, w = set(old.splitlines()), set(new.splitlines())
    added = [l for l in new.splitlines() if l not in o][:n]
    removed = [l for l in old.splitlines() if l not in w][:n]
    return added, removed

async def run_watch(row, announce=True):
    wid, gid, cid, url, what, every, last_value, last_hash = row
    try:
        val, h = await check_value(url, what)
    except Exception as e:
        db.execute("UPDATE watches SET last_check=?, fails=fails+1 WHERE id=?", (time.time(), wid))
        db.commit()
        log.info("watch %s failed: %s", wid, e)
        return None
    changed = bool(last_hash) and h != last_hash and not (what and val == "NOT_FOUND")
    db.execute("UPDATE watches SET last_check=?, last_value=?, last_hash=?, fails=0 WHERE id=?",
               (time.time(), val, h, wid))
    db.commit()
    if changed and announce:
        ch = client.get_channel(cid)
        if ch:
            if what:
                desc = f"**{what}**\n~~{last_value[:300]}~~\n➡️ **{val}**"
            else:
                added, removed = _diff(last_value, val)
                desc = ("**New / changed:**\n" + "\n".join(f"+ {l[:150]}" for l in added) if added else "") + \
                       ("\n**Gone:**\n" + "\n".join(f"- {l[:150]}" for l in removed) if removed else "")
            e = embed("🔔 Page changed", f"<{url}>\n\n{desc}"[:4000])
            e.set_footer(text=f"{FOOTER} · watch #{wid} · checks every {every} min")
            await safe_send(ch, embed=e)
    return val

@on_startup
async def watch_loop():
    await client.wait_until_ready()
    await asyncio.sleep(30)
    while not client.is_closed():
        now = time.time()
        rows = db.execute("SELECT id, guild_id, channel_id, url, what, every, last_value, last_hash FROM watches "
                          "WHERE last_check + every * 60 <= ? AND fails < 12 ORDER BY last_check LIMIT 5",
                          (now,)).fetchall()
        for r in rows:
            await run_watch(r)
            await asyncio.sleep(2)
        await asyncio.sleep(60)

def watch_count(gid):
    return db.execute("SELECT COUNT(*) FROM watches WHERE guild_id=?", (gid,)).fetchone()[0]

async def add_watch(gid, channel_id, url, what, every, by_id):
    tier = store.guild(gid).get("tier", "free")
    if watch_count(gid) >= MAX_WATCHES.get(tier, 5):
        raise RuntimeError(f"this server can watch {MAX_WATCHES.get(tier, 5)} pages at most — remove one first")
    every = max(MIN_EVERY, min(int(every or 60), 1440))
    await web.check_url(url, (80, 443))
    cur = db.execute("INSERT INTO watches (guild_id, channel_id, url, what, every, by_id, created) VALUES (?,?,?,?,?,?,?)",
                     (gid, channel_id, url, what, every, by_id, time.time()))
    db.commit()
    wid = cur.lastrowid
    first = await run_watch((wid, gid, channel_id, url, what, every, "", ""), announce=False)
    return wid, every, first

@tool("watch_page", "watch_page | <url> | <what to watch, e.g. 'price of RTX 4060' (optional)> | <every N minutes> "
      "| <#channel, optional>", "watch a web page and post here when it changes (price, stock, status, news)",
      when=lambda c: bool(c.gid and c.guild and is_admin(c.user)), progress="🔔 Setting up the watch…")
async def t_watch(c, a):
    from core import find_channel
    a = (a + ["", "", "", ""])[:4]
    ch = (find_channel(c.guild, a[3]) if a[3] else None) or c.channel
    try:
        wid, every, first = await add_watch(c.gid, ch.id, a[0].strip(), a[1].strip(),
                                            int(a[2]) if a[2].strip().isdigit() else 60, c.user.id)
    except Exception as e:
        return Result(f"Couldn't set up the watch: {e}")
    return Result(f"Watch #{wid} set: {a[0]} every {every} min, changes posted in #{ch.name}. "
                  f"Current value: {(first or 'checked')[:300]}", "🔔 watching")

# ---------- slash commands ----------
@tree.command(name="extract", description="Collect data from a website into a table + CSV (works on JS/React sites)")
@app_commands.describe(url="Page address", what="What to collect, e.g. 'products with price and stock'")
async def extract_cmd(inter: discord.Interaction, url: str, what: Optional[str] = None):
    if inter.guild_id and not feature(inter.guild_id, "browse"):
        return await inter.response.send_message("Browsing is turned off in this server.", ephemeral=True)
    if inter.guild_id and not store.quota(inter.guild_id, inter.user.id, "browse"):
        return await inter.response.send_message("📉 Today's browsing limit is used up.", ephemeral=True)
    await inter.response.defer(thinking=True)
    try:
        final, title, cols, rows, note = await extract(url, what or "")
    except web.BlockedURL:
        return await inter.followup.send("⚠️ That address isn't allowed.")
    except Exception as e:
        return await inter.followup.send(f"⚠️ Couldn't collect data: {e}")
    if not rows:
        return await inter.followup.send(f"Nothing matching found on <{final}>. {note}")
    e = embed(f"🕸️ {title or 'Collected data'}"[:250], f"<{final}>\n**{len(rows)} rows** · "
              f"{', '.join(cols)}\n{preview(cols, rows)}" + (f"\n-# {note}" if note else ""))
    e.set_footer(text=FOOTER + " · full data in the CSV")
    await inter.followup.send(embed=e, file=discord.File(io.BytesIO(to_csv(cols, rows)), filename=f"{_slug(final)}.csv"))

watch_grp = app_commands.Group(name="watch", description="Watch web pages and get told when they change",
                               guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

@watch_grp.command(name="add", description="Watch a page (price, stock, status…) and post changes in a channel")
@app_commands.describe(url="Page address", what="What to watch (empty = any change in the page text)",
                       every="Check every N minutes (15–1440, default 60)", channel="Where to post (default: here)")
async def watch_add(inter: discord.Interaction, url: str, what: Optional[str] = None,
                    every: app_commands.Range[int, 15, 1440] = 60, channel: Optional[discord.TextChannel] = None):
    if not is_admin(inter.user):
        return await inter.response.send_message("You need Manage Server.", ephemeral=True)
    await inter.response.defer(thinking=True, ephemeral=True)
    ch = channel or inter.channel
    try:
        wid, every, first = await add_watch(inter.guild_id, ch.id, url, (what or "")[:200], every, inter.user.id)
    except Exception as e:
        return await inter.followup.send(f"⚠️ {e}", ephemeral=True)
    now = (first or "")[:300] if what else "page saved"
    await inter.followup.send(f"🔔 Watch **#{wid}** on <{url}> every **{every} min** → {ch.mention}\nNow: {now}",
                              ephemeral=True)

@watch_grp.command(name="list", description="Pages this server is watching")
async def watch_list(inter: discord.Interaction):
    rows = db.execute("SELECT id, url, what, every, channel_id, last_value, fails FROM watches WHERE guild_id=?",
                      (inter.guild_id,)).fetchall()
    lines = [f"**#{i}** <{u}> · {w or 'any change'} · {e} min → <#{c}>"
             + (f" · now: `{v[:60]}`" if w and v else "") + (" · ⚠️ failing" if f >= 3 else "")
             for i, u, w, e, c, v, f in rows]
    await inter.response.send_message(embed=embed("🔔 Watched pages", "\n".join(lines) or "None. `/watch add`"),
                                      ephemeral=True)

@watch_grp.command(name="remove", description="Stop watching a page")
async def watch_remove(inter: discord.Interaction, watch_id: int):
    if not is_admin(inter.user):
        return await inter.response.send_message("You need Manage Server.", ephemeral=True)
    n = db.execute("DELETE FROM watches WHERE id=? AND guild_id=?", (watch_id, inter.guild_id)).rowcount
    db.commit()
    await inter.response.send_message("🗑️ Removed." if n else "No such watch.", ephemeral=True)

tree.add_command(watch_grp)


# ---------------------------------------------------------------- page screenshots
@tool("screenshot", "screenshot | <url>",
      "take a picture of a web page (after its JavaScript loads) and post it; also lets you see pages that have "
      "no readable text", when=lambda c: feature(c.gid, "browse") if c.gid else True,
      progress="📸 Taking a screenshot of **{0}**…")
async def t_screenshot(c, a):
    m = web.URL_RE.search(a[0] if a else "")
    if not m:
        return Result("Give me a link to screenshot.")
    if c.gid and not store.quota(c.gid, c.user.id, "browse"):
        return Result("Today's browsing limit for this server is used up.")
    try:
        final, png = await web.screenshot(m.group())
    except web.BlockedURL:
        return Result("That address isn't allowed (private or local network).")
    except Exception as e:
        return Result(f"Couldn't take a screenshot: {e}")
    f = discord.File(io.BytesIO(png), filename=f"{_slug(final)}.png")
    c.after_send.append((f"📸 <{final}>", None, {"file": f}))
    return Result(f"Screenshot of {final} taken and attached below your reply. Describe what's on it only if asked "
                  f"or if it helps answer.", "📸 screenshot")
