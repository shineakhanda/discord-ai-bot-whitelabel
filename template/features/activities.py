"""Keep community servers active: question of the day, daily trivia with points, mini-game channels
(counting, word chain, guess the number), giveaways with buttons, and polls. Each is off until an admin
turns it on with /activity …"""
import json, random, re, time, asyncio
from datetime import datetime, timedelta
from typing import Literal, Optional

import discord
from discord import app_commands

import store
from store import db, log
from core import client, tree, embed, is_admin, can_announce, safe_send, on_startup, COLOR
from agent import tool, Result, one_shot
from timeparse import tzinfo, duration

FOOTER = "__BOT_NAME__ activities"

db.executescript("""
CREATE TABLE IF NOT EXISTS points (guild_id INTEGER, user_id INTEGER, week TEXT, pts INTEGER,
                                   PRIMARY KEY (guild_id, user_id, week));
CREATE TABLE IF NOT EXISTS giveaways (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
                                      message_id INTEGER, prize TEXT, winners INTEGER, ends REAL, host_id INTEGER,
                                      entries TEXT DEFAULT '[]', ended INTEGER DEFAULT 0, winner_ids TEXT DEFAULT '[]');
CREATE INDEX IF NOT EXISTS gw_msg ON giveaways (message_id);
""")
db.commit()

DEFAULTS = {
    "qotd": {"channel": None, "time": "20:00", "last": "", "role": None},
    "trivia": {"channel": None, "time": "21:00", "last": "", "role": None, "topic": ""},
    "counting": {"channel": None, "n": 0, "last_user": None, "best": 0},
    "wordchain": {"channel": None, "last": "", "last_user": None, "used": [], "streak": 0, "best": 0},
    "guess": {"channel": None, "target": None, "max": 100, "tries": 0},
    "weekly": {"last_week": ""},
}

def acts(gid):
    g = store.guild(gid)
    a = g.setdefault("activities", {})
    for k, v in DEFAULTS.items():
        cur = a.setdefault(k, json.loads(json.dumps(v)))
        for kk, vv in v.items():
            cur.setdefault(kk, vv)
    return a

def save(gid):
    store.save_guild(gid)

def local_now(gid):
    return datetime.now(tzinfo(store.guild(gid)["timezone"]))

def week_key(dt=None):
    y, w, _ = (dt or datetime.utcnow()).isocalendar()
    return f"{y}-W{w:02d}"

# ---------- points ----------
def add_points(gid, uid, pts):
    wk = week_key()
    db.execute("INSERT INTO points VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,week) DO UPDATE SET pts=pts+?",
               (gid, uid, wk, pts, pts))
    db.commit()

def leaderboard(gid, week=None, limit=10):
    if week == "all":
        rows = db.execute("SELECT user_id, SUM(pts) s FROM points WHERE guild_id=? GROUP BY user_id ORDER BY s DESC "
                          "LIMIT ?", (gid, limit)).fetchall()
    else:
        rows = db.execute("SELECT user_id, pts FROM points WHERE guild_id=? AND week=? ORDER BY pts DESC LIMIT ?",
                          (gid, week or week_key(), limit)).fetchall()
    return rows

def board_embed(gid, week=None, title="🏆 Weekly leaderboard"):
    rows = leaderboard(gid, week)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{u}> — **{p}** pts" for i, (u, p) in enumerate(rows)]
    e = embed(title, "\n".join(lines) or "No points yet. Play trivia and the mini-games to get on the board!")
    e.set_footer(text=f"{FOOTER} · trivia, guess the number and word chain give points")
    return e

# ---------- AI content ----------
def _server_flavor(gid):
    g = store.guild(gid)
    kind = g.get("type", "general")
    lang = g.get("lang", "auto")
    lang_txt = {"bn": "Bangla (Bengali script)", "banglish": "Banglish (Bangla in English letters)",
                "en": "English"}.get(lang, "simple English that Bangladeshi members understand")
    return kind, lang_txt

FALLBACK_QOTD = ["What's one small thing that made you happy this week?", "Which game could you play forever?",
                 "If you could master one skill overnight, what would it be?", "Tea or coffee — and why?",
                 "What's the best advice you've ever been given?", "Which song is stuck in your head right now?",
                 "Mountains or the sea for a holiday?", "What's a hobby you want to start this year?"]

async def make_qotd(gid):
    kind, lang = _server_flavor(gid)
    try:
        q = await one_shot(f"Write ONE fun, friendly 'question of the day' for a Discord {kind} server. "
                           f"Language: {lang}. Easy to answer, sparks conversation, safe for all ages, no politics "
                           f"or religion. Output only the question, max 25 words, at most one emoji.",
                           "question of the day", max_tokens=80, temperature=1.0)
        q = q.strip().strip('"').splitlines()[0][:300]
        if len(q) > 8:
            return q
    except Exception as e:
        log.warning("qotd ai failed: %s", e)
    return random.choice(FALLBACK_QOTD)

FALLBACK_TRIVIA = [
    {"q": "What is the capital of Bangladesh?", "o": ["Chattogram", "Dhaka", "Khulna", "Sylhet"], "a": 1,
     "e": "Dhaka is the capital and largest city."},
    {"q": "Which planet is known as the Red Planet?", "o": ["Venus", "Jupiter", "Mars", "Mercury"], "a": 2,
     "e": "Iron oxide dust makes Mars look red."},
    {"q": "In Minecraft, what do you need to mine diamonds?", "o": ["Stone pickaxe", "Iron pickaxe", "Gold pickaxe",
     "Wooden pickaxe"], "a": 1, "e": "An iron (or better) pickaxe is required."},
    {"q": "How many players are on the field for one football (soccer) team?", "o": ["9", "10", "11", "12"], "a": 2,
     "e": "11 players including the goalkeeper."},
    {"q": "What does CPU stand for?", "o": ["Central Processing Unit", "Computer Power Unit", "Core Program Utility",
     "Central Power Usage"], "a": 0, "e": "The CPU runs a computer's instructions."},
    {"q": "Which is the national flower of Bangladesh?", "o": ["Rose", "Lotus", "Water lily (Shapla)", "Jasmine"],
     "a": 2, "e": "The water lily, called Shapla."},
]

async def make_trivia(gid, topic=""):
    kind, lang = _server_flavor(gid)
    topic = topic or acts(gid)["trivia"].get("topic") or {"minecraft": "Minecraft", "game": "video games",
                                                         "store": "general knowledge and tech",
                                                         }.get(kind, "general knowledge, Bangladesh, games, tech, sports")
    try:
        raw = await one_shot(
            f"Create ONE multiple-choice trivia question about: {topic}. Language: {lang}. Medium difficulty, "
            "one clearly correct answer, facts you are sure about (no recent events). Reply with ONLY JSON: "
            '{"q": "question", "o": ["A", "B", "C", "D"], "a": <index 0-3 of correct>, "e": "one-line explanation"}',
            "trivia", max_tokens=300, temperature=0.9, smart=True)
        m = re.search(r"\{.*\}", raw, re.S)
        t = json.loads(m.group(0))
        if (isinstance(t.get("o"), list) and len(t["o"]) == 4 and isinstance(t.get("a"), int)
                and 0 <= t["a"] < 4 and t.get("q")):
            t["o"] = [str(x)[:80] for x in t["o"]]
            return t
    except Exception as e:
        log.warning("trivia ai failed: %s", e)
    return random.choice(FALLBACK_TRIVIA)

# =====================================================================
# QOTD & trivia posting
# =====================================================================
async def post_qotd(gid, channel=None):
    a = acts(gid)["qotd"]
    ch = channel or client.get_channel(a["channel"] or 0)
    if not ch:
        return None
    q = await make_qotd(gid)
    e = embed("💬 Question of the Day", f"**{q}**\n\nReply in the thread below 👇")
    e.set_footer(text=f"{FOOTER} · {local_now(gid):%d %b %Y}")
    msg = await safe_send(ch, content=f"<@&{a['role']}>" if a["role"] else None, embed=e,
                          allowed_mentions=discord.AllowedMentions(roles=True))
    if msg:
        try:
            await msg.create_thread(name=f"QOTD {local_now(gid):%d %b}", auto_archive_duration=1440)
        except discord.HTTPException:
            pass
    return msg

class TriviaView(discord.ui.View):
    LETTERS = "ABCD"

    def __init__(self, gid, t, minutes=30):
        super().__init__(timeout=minutes * 60)
        self.gid, self.t = gid, t
        self.answers = {}                          # user_id -> (choice, seconds)
        self.start = time.time()
        self.message = None
        for i, opt in enumerate(t["o"]):
            b = discord.ui.Button(label=f"{self.LETTERS[i]}. {opt}"[:80], style=discord.ButtonStyle.secondary,
                                  custom_id=f"trv:{int(self.start)}:{i}")
            b.callback = self._pick(i)
            self.add_item(b)

    def _pick(self, i):
        async def cb(inter: discord.Interaction):
            if inter.user.id in self.answers:
                return await inter.response.send_message("You already answered this one 🙂", ephemeral=True)
            self.answers[inter.user.id] = (i, time.time() - self.start)
            await inter.response.send_message(f"Locked in **{self.LETTERS[i]}**. Answer is revealed when time's up!",
                                              ephemeral=True)
        return cb

    async def on_timeout(self):
        await finish_trivia(self)

async def finish_trivia(view):
    t, gid = view.t, view.gid
    right = sorted(((s, u) for u, (c, s) in view.answers.items() if c == t["a"]))
    bonus = [5, 3, 1]
    lines = []
    for rank, (secs, uid) in enumerate(right):
        pts = 10 + (bonus[rank] if rank < 3 else 0)
        add_points(gid, uid, pts)
        if rank < 10:
            lines.append(f"`{secs:4.0f}s` <@{uid}> +{pts}")
    e = embed("🧠 Trivia — time's up!",
              f"**{t['q']}**\n✅ **{TriviaView.LETTERS[t['a']]}. {t['o'][t['a']]}**\n-# {t.get('e', '')}\n\n"
              + (f"**{len(right)}/{len(view.answers)} got it right**\n" + "\n".join(lines) if view.answers
                 else "Nobody answered this time."))
    e.set_footer(text=f"{FOOTER} · /leaderboard to see the week's top players")
    if view.message:
        try:
            await view.message.edit(embed=e, view=None)
        except discord.HTTPException:
            pass

async def post_trivia(gid, channel=None, topic="", minutes=30):
    a = acts(gid)["trivia"]
    ch = channel or client.get_channel(a["channel"] or 0)
    if not ch:
        return None
    t = await make_trivia(gid, topic)
    view = TriviaView(gid, t, minutes)
    e = embed("🧠 Daily Trivia", f"**{t['q']}**\n\n" + "\n".join(f"**{TriviaView.LETTERS[i]}.** {o}"
                                                               for i, o in enumerate(t["o"]))
              + f"\n\n⏱️ Ends <t:{int(time.time() + minutes * 60)}:R> · correct = 10 pts, fastest 3 get a bonus")
    e.set_footer(text=FOOTER)
    view.message = await safe_send(ch, content=f"<@&{a['role']}>" if a["role"] else None, embed=e, view=view,
                                   allowed_mentions=discord.AllowedMentions(roles=True))
    return view.message

def _due(gid, item):
    """True once per local day, at/after the set HH:MM."""
    now = local_now(gid)
    try:
        hh, mm = (int(x) for x in item["time"].split(":"))
    except ValueError:
        hh, mm = 20, 0
    today = now.strftime("%Y-%m-%d")
    return item["last"] != today and (now.hour, now.minute) >= (hh, mm), today

@on_startup
async def activity_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        for g in list(client.guilds):
            try:
                a = acts(g.id)
                for kind, poster in (("qotd", post_qotd), ("trivia", post_trivia)):
                    item = a[kind]
                    if not item["channel"]:
                        continue
                    due, today = _due(g.id, item)
                    if due:
                        item["last"] = today
                        save(g.id)
                        await poster(g.id)
                # weekly winners on Monday (server time), in the trivia channel
                now = local_now(g.id)
                wk = week_key(now)
                if a["trivia"]["channel"] and now.weekday() == 0 and a["weekly"]["last_week"] != wk:
                    a["weekly"]["last_week"] = wk
                    save(g.id)
                    last = week_key(now - timedelta(days=7))
                    if leaderboard(g.id, last, 1):
                        ch = client.get_channel(a["trivia"]["channel"])
                        if ch:
                            await safe_send(ch, embed=board_embed(g.id, last, "🏆 Last week's champions"))
            except Exception as e:
                log.warning("activity loop %s: %s", g.id, e)
        await asyncio.sleep(60)

# =====================================================================
# mini-game channels (called from chat.on_message before the AI)
# =====================================================================
WORD_RE = re.compile(r"^[a-zA-Z]{2,30}$")

async def _react(msg, emoji):
    try:
        await msg.add_reaction(emoji)
    except discord.HTTPException:
        pass

async def _note(msg, text, after=8):
    try:
        await msg.reply(text, delete_after=after, mention_author=False)
    except discord.HTTPException:
        pass

async def handle_message(msg):
    """Returns True when the message belonged to a game channel (so the AI stays quiet)."""
    if not msg.guild:
        return False
    a = store.guild(msg.guild.id).get("activities")
    if not a:
        return False
    cid, gid, uid = msg.channel.id, msg.guild.id, msg.author.id
    text = msg.content.strip()
    c = a.get("counting") or {}
    if c.get("channel") == cid:
        if not re.fullmatch(r"\d{1,9}", text):
            return True                            # chatter in the counting channel is ignored
        n = int(text)
        if n == c["n"] + 1 and uid != c["last_user"]:
            c["n"], c["last_user"] = n, uid
            if n > c["best"]:
                c["best"] = n
            save(gid)
            await _react(msg, "🎉" if n % 100 == 0 else "✅")
        else:
            why = "you can't count twice in a row" if uid == c["last_user"] else f"next number was **{c['n'] + 1}**"
            old = c["n"]
            c["n"], c["last_user"] = 0, None
            save(gid)
            await _react(msg, "❌")
            await safe_send(msg.channel, f"💥 {msg.author.mention} broke the count at **{old}** ({why}). "
                                         f"Start again from **1**! Best: **{c['best']}**")
        return True
    w = a.get("wordchain") or {}
    if w.get("channel") == cid:
        word = text.lower()
        if not WORD_RE.fullmatch(word):
            return True
        if uid == w["last_user"]:
            await _react(msg, "⏳")
            await _note(msg, "Wait for someone else to play first 🙂")
            return True
        if w["last"] and word[0] != w["last"][-1]:
            await _react(msg, "❌")
            await _note(msg, f"Must start with **{w['last'][-1].upper()}** (last word: `{w['last']}`).")
            return True
        if word in w["used"]:
            await _react(msg, "🔁")
            await _note(msg, f"`{word}` was already used — try another.")
            return True
        w["last"], w["last_user"] = word, uid
        w["used"] = (w["used"] + [word])[-500:]
        w["streak"] += 1
        w["best"] = max(w["best"], w["streak"])
        save(gid)
        add_points(gid, uid, 1)
        await _react(msg, "✅")
        if w["streak"] % 50 == 0:
            await safe_send(msg.channel, f"🔥 **{w['streak']} words** in a row! Next letter: **{word[-1].upper()}**")
        return True
    gs = a.get("guess") or {}
    if gs.get("channel") == cid:
        if not re.fullmatch(r"\d{1,6}", text):
            return True
        if not gs["target"]:
            gs["target"], gs["tries"] = random.randint(1, gs["max"]), 0
        n = int(text)
        gs["tries"] += 1
        if n == gs["target"]:
            pts = max(5, 25 - gs["tries"])
            add_points(gid, uid, pts)
            await _react(msg, "🎯")
            await safe_send(msg.channel, f"🎉 {msg.author.mention} guessed **{n}** after {gs['tries']} tries! +{pts} pts\n"
                                         f"New number picked between **1** and **{gs['max']}** — go!")
            gs["target"], gs["tries"] = random.randint(1, gs["max"]), 0
        else:
            await _react(msg, "⬆️" if n < gs["target"] else "⬇️")
        save(gid)
        return True
    return False

# =====================================================================
# giveaways (buttons survive restarts)
# =====================================================================
class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter", emoji="🎉", style=discord.ButtonStyle.success, custom_id="__bot_slug__:gw_enter")
    async def enter(self, inter: discord.Interaction, _):
        row = db.execute("SELECT id, entries, ended, ends FROM giveaways WHERE message_id=?",
                         (inter.message.id,)).fetchone()
        if not row or row[2] or row[3] < time.time():
            return await inter.response.send_message("This giveaway has ended.", ephemeral=True)
        entries = set(json.loads(row[1]))
        if inter.user.id in entries:
            entries.discard(inter.user.id)
            note = "You left the giveaway."
        else:
            entries.add(inter.user.id)
            note = "🎉 You're in! Good luck."
        db.execute("UPDATE giveaways SET entries=? WHERE id=?", (json.dumps(sorted(entries)), row[0]))
        db.commit()
        await inter.response.send_message(f"{note} ({len(entries)} entries)", ephemeral=True)

def _gw_embed(prize, ends, winners, host_id, entries=0, done_text=None):
    desc = (f"### {prize}\n" + (done_text or f"Press **🎉 Enter** to join!\nEnds <t:{int(ends)}:R> (<t:{int(ends)}:f>)")
            + f"\n\n**Winners:** {winners} · **Hosted by:** <@{host_id}>" + (f" · **Entries:** {entries}" if entries else ""))
    e = embed("🎁 GIVEAWAY" if not done_text else "🎁 GIVEAWAY ENDED", desc)
    e.set_footer(text=FOOTER)
    return e

async def start_giveaway(gid, channel, prize, seconds, winners, host_id):
    ends = time.time() + seconds
    msg = await channel.send(embed=_gw_embed(prize, ends, winners, host_id), view=GiveawayView())
    db.execute("INSERT INTO giveaways (guild_id, channel_id, message_id, prize, winners, ends, host_id) "
               "VALUES (?,?,?,?,?,?,?)", (gid, channel.id, msg.id, prize[:200], winners, ends, host_id))
    db.commit()
    return msg

async def end_giveaway(gid_row, reroll=False):
    gid, gw_id, cid, mid, prize, n, host, entries = gid_row
    entries = json.loads(entries)
    ch = client.get_channel(cid)
    winners = random.sample(entries, min(n, len(entries))) if entries else []
    db.execute("UPDATE giveaways SET ended=1, winner_ids=? WHERE id=?", (json.dumps(winners), gw_id))
    db.commit()
    if not ch:
        return winners
    text = ("**Winner" + ("s" if len(winners) > 1 else "") + ":** " + ", ".join(f"<@{w}>" for w in winners)
            if winners else "No one entered 😢")
    try:
        msg = await ch.fetch_message(mid)
        await msg.edit(embed=_gw_embed(prize, time.time(), n, host, len(entries), text), view=None)
    except discord.HTTPException:
        pass
    if winners:
        await safe_send(ch, f"🎉 {'Reroll! ' if reroll else ''}Congratulations {', '.join(f'<@{w}>' for w in winners)} — "
                            f"you won **{prize}**! Contact <@{host}> to claim.",
                        allowed_mentions=discord.AllowedMentions(users=True))
    return winners

@on_startup
async def giveaway_loop():
    await client.wait_until_ready()
    client.add_view(GiveawayView())
    while not client.is_closed():
        try:
            rows = db.execute("SELECT guild_id, id, channel_id, message_id, prize, winners, host_id, entries "
                              "FROM giveaways WHERE ended=0 AND ends<=?", (time.time(),)).fetchall()
            for r in rows:
                await end_giveaway(r)
        except Exception as e:
            log.warning("giveaway loop: %s", e)
        await asyncio.sleep(20)

# =====================================================================
# polls (Discord's native polls, with a reaction fallback)
# =====================================================================
NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

async def make_poll(channel, question, options, hours=24, multi=False, author=None):
    options = [o.strip()[:55] for o in options if o.strip()][:10]
    if len(options) < 2:
        raise ValueError("a poll needs at least 2 options")
    Poll = getattr(discord, "Poll", None)
    if Poll is not None:
        p = Poll(question=question[:300], duration=timedelta(hours=max(1, min(hours, 768))), multiple=multi)
        for o in options:
            p.add_answer(text=o)
        return await channel.send(poll=p)
    e = embed(f"📊 {question[:250]}", "\n".join(f"{NUM[i]} {o}" for i, o in enumerate(options)))
    e.set_footer(text=FOOTER + (f" · by {author}" if author else ""))
    msg = await channel.send(embed=e)
    for i in range(len(options)):
        await _react(msg, NUM[i])
    return msg

# =====================================================================
# AI tools
# =====================================================================
def _staff(c):
    return bool(c.gid and c.guild and can_announce(c.user, c.gid))

def _chan(c, text):
    from core import find_channel
    return (find_channel(c.guild, text) if text else None) or c.channel

@tool("giveaway_start", "giveaway_start | <prize> | <duration e.g. 1d, 2h> | <winners> | <#channel, optional>",
      "start a giveaway with an Enter button; winners are picked automatically", when=_staff,
      progress="🎁 Starting the giveaway…")
async def t_giveaway(c, a):
    a = (a + ["", "", "", ""])[:4]
    secs = duration(a[1] or "1d")
    if not a[0] or not secs:
        return Result("Usage: giveaway_start | <prize> | <duration like 2h or 3d> | <winners>")
    ch = _chan(c, a[3])
    n = int(a[2]) if a[2].strip().isdigit() else 1
    await start_giveaway(c.gid, ch, a[0], min(secs, 30 * 86400), max(1, min(n, 20)), c.user.id)
    return Result(f"Giveaway for '{a[0]}' started in #{ch.name}, {n} winner(s), ends in {a[1]}.", "🎁 giveaway started")

@tool("poll_create", "poll_create | <question> | <option 1> ; <option 2> ; … | <hours, optional> | <#channel, optional>",
      "post a poll", when=_staff, progress="📊 Creating the poll…")
async def t_poll(c, a):
    a = (a + ["", "", "", ""])[:4]
    opts = [o for o in re.split(r"[;,\n]", a[1]) if o.strip()]
    try:
        hours = int(a[2]) if a[2].strip().isdigit() else 24
        ch = _chan(c, a[3])
        await make_poll(ch, a[0], opts, hours, author=c.user.display_name)
    except (ValueError, discord.HTTPException) as e:
        return Result(f"Couldn't create the poll: {e}")
    return Result(f"Poll posted in #{ch.name}.", "📊 poll posted")

@tool("activity_now", "activity_now | <trivia|qotd> | <topic, optional>",
      "post a trivia question or a question of the day right now in this channel", when=_staff,
      progress="🧠 Making a question…")
async def t_activity(c, a):
    a = (a + ["", ""])[:2]
    if a[0].strip().lower().startswith("q"):
        await post_qotd(c.gid, c.channel)
    else:
        await post_trivia(c.gid, c.channel, a[1].strip())
    return Result("Posted in this channel. Don't repeat it.", "🧠 posted")

@tool("leaderboard", "leaderboard", "this week's activity points top 10", when=lambda c: bool(c.gid))
async def t_board(c, a):
    rows = leaderboard(c.gid)
    return Result("This week: " + ", ".join(f"#{i + 1} <@{u}> {p}pts" for i, (u, p) in enumerate(rows)) or "No points yet.")

# =====================================================================
# slash commands
# =====================================================================
act_grp = app_commands.Group(name="activity", description="Keep your community active: QOTD, trivia, mini-games",
                             guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

async def _admin(inter):
    if not is_admin(inter.user):
        await inter.response.send_message("You need **Manage Server** for this.", ephemeral=True)
        return False
    return True

def _hhmm(s):
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", (s or "").lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None

@act_grp.command(name="qotd", description="Daily question of the day (with an answers thread)")
@app_commands.describe(channel="Where to post (leave empty to turn off)", time="Server time, e.g. 20:00 or 8pm",
                       ping="Role to ping (optional)")
async def act_qotd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                   time: Optional[str] = None, ping: Optional[discord.Role] = None):
    if not await _admin(inter):
        return
    q = acts(inter.guild_id)["qotd"]
    q["channel"] = channel.id if channel else None
    if time:
        t = _hhmm(time)
        if not t:
            return await inter.response.send_message("⚠️ Time like `20:00` or `8pm`.", ephemeral=True)
        q["time"] = t
    q["role"] = ping.id if ping else None
    save(inter.guild_id)
    await inter.response.send_message(f"✅ QOTD {'in ' + channel.mention + ' daily at **' + q['time'] + '**' if channel else 'turned off'}.",
                                      ephemeral=True)

@act_grp.command(name="trivia", description="Daily trivia with points and a weekly leaderboard")
@app_commands.describe(channel="Where to post (leave empty to turn off)", time="Server time, e.g. 21:00 or 9pm",
                       topic="Topic (e.g. Minecraft, football, Bangladesh). Empty = fits your server type",
                       ping="Role to ping (optional)")
async def act_trivia(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                     time: Optional[str] = None, topic: Optional[str] = None, ping: Optional[discord.Role] = None):
    if not await _admin(inter):
        return
    t = acts(inter.guild_id)["trivia"]
    t["channel"] = channel.id if channel else None
    if time:
        hh = _hhmm(time)
        if not hh:
            return await inter.response.send_message("⚠️ Time like `21:00` or `9pm`.", ephemeral=True)
        t["time"] = hh
    if topic is not None:
        t["topic"] = topic[:80]
    t["role"] = ping.id if ping else None
    save(inter.guild_id)
    await inter.response.send_message(f"✅ Trivia {'in ' + channel.mention + ' daily at **' + t['time'] + '**' if channel else 'turned off'}"
                                      + (f" · topic: {t['topic']}" if t["topic"] else "") +
                                      ". Winners are announced every Monday.", ephemeral=True)

@act_grp.command(name="game", description="Turn a channel into a mini-game channel (or turn it off)")
@app_commands.describe(game="counting · word chain · guess the number", channel="Leave empty to turn this game off",
                       max_number="Guess the number: highest number (default 100)")
async def act_game(inter: discord.Interaction, game: Literal["counting", "wordchain", "guess"],
                   channel: Optional[discord.TextChannel] = None, max_number: Optional[int] = None):
    if not await _admin(inter):
        return
    a = acts(inter.guild_id)
    a[game]["channel"] = channel.id if channel else None
    if game == "guess":
        a["guess"]["max"] = max(10, min(max_number or a["guess"]["max"], 100000))
        a["guess"]["target"] = None
    save(inter.guild_id)
    if not channel:
        return await inter.response.send_message(f"✅ {game} turned off.", ephemeral=True)
    intro = {"counting": "🔢 **Counting!** Count up one number at a time, no one counts twice in a row. Start: **1**",
             "wordchain": "🔤 **Word chain!** Each word must start with the last letter of the previous one. "
                          "No repeats. English letters only. Start with any word!",
             "guess": f"🎯 **Guess the number!** I picked a number between **1** and **{a['guess']['max']}**. "
                      "Type a number — I'll react ⬆️ higher or ⬇️ lower."}[game]
    await safe_send(channel, embed=embed(None, intro))
    await inter.response.send_message(f"✅ {channel.mention} is now the **{game}** channel. (I stay quiet there.)",
                                      ephemeral=True)

@act_grp.command(name="now", description="Post a trivia question or QOTD right now in this channel")
async def act_now(inter: discord.Interaction, kind: Literal["trivia", "qotd"], topic: Optional[str] = None):
    if not (is_admin(inter.user) or can_announce(inter.user, inter.guild_id)):
        return await inter.response.send_message("You need Manage Server or the announcer role.", ephemeral=True)
    await inter.response.send_message("⏳ Making it…", ephemeral=True)
    if kind == "qotd":
        await post_qotd(inter.guild_id, inter.channel)
    else:
        await post_trivia(inter.guild_id, inter.channel, topic or "")

@act_grp.command(name="status", description="Which activities are on")
async def act_status(inter: discord.Interaction):
    a = acts(inter.guild_id)
    ch = lambda x: f"<#{x}>" if x else "off"
    e = embed("🎮 Activities", None)
    e.add_field(name="Question of the day", value=f"{ch(a['qotd']['channel'])} · {a['qotd']['time']}")
    e.add_field(name="Trivia", value=f"{ch(a['trivia']['channel'])} · {a['trivia']['time']}"
                + (f" · {a['trivia']['topic']}" if a['trivia']['topic'] else ""))
    e.add_field(name="Counting", value=f"{ch(a['counting']['channel'])} · now {a['counting']['n']} · best {a['counting']['best']}")
    e.add_field(name="Word chain", value=f"{ch(a['wordchain']['channel'])} · best streak {a['wordchain']['best']}")
    e.add_field(name="Guess the number", value=f"{ch(a['guess']['channel'])} · 1–{a['guess']['max']}")
    e.set_footer(text=FOOTER + " · giveaways: /giveaway · polls: /poll")
    await inter.response.send_message(embed=e, ephemeral=True)

tree.add_command(act_grp)

@tree.command(name="leaderboard", description="Top activity players (trivia & games)")
@app_commands.guild_only()
async def leaderboard_cmd(inter: discord.Interaction, period: Literal["this week", "all time"] = "this week"):
    await inter.response.send_message(embed=board_embed(inter.guild_id, "all" if period == "all time" else None,
                                                        "🏆 All-time leaderboard" if period == "all time"
                                                        else "🏆 This week's leaderboard"))

gw_grp = app_commands.Group(name="giveaway", description="Giveaways with an Enter button", guild_only=True)

def _gw_ok(inter):
    return is_admin(inter.user) or can_announce(inter.user, inter.guild_id)

@gw_grp.command(name="start", description="Start a giveaway")
@app_commands.describe(prize="What's the prize", length="e.g. 30m, 2h, 3d", winners="How many winners",
                       channel="Where (default: here)")
async def gw_start(inter: discord.Interaction, prize: app_commands.Range[str, 1, 200], length: str,
                   winners: app_commands.Range[int, 1, 20] = 1, channel: Optional[discord.TextChannel] = None):
    if not _gw_ok(inter):
        return await inter.response.send_message("You need Manage Server or the announcer role.", ephemeral=True)
    secs = duration(length)
    if not secs or secs < 60:
        return await inter.response.send_message("⚠️ Length like `30m`, `2h` or `3d` (at least 1 minute).",
                                                 ephemeral=True)
    ch = channel or inter.channel
    await start_giveaway(inter.guild_id, ch, prize, min(secs, 30 * 86400), winners, inter.user.id)
    await inter.response.send_message(f"🎁 Giveaway started in {ch.mention}!", ephemeral=True)

def _gw_find(gid, message_id):
    q = "SELECT guild_id, id, channel_id, message_id, prize, winners, host_id, entries FROM giveaways WHERE guild_id=? "
    if message_id:
        return db.execute(q + "AND message_id=?", (gid, int(message_id))).fetchone()
    return db.execute(q + "ORDER BY id DESC LIMIT 1", (gid,)).fetchone()

@gw_grp.command(name="end", description="End a giveaway now (default: the latest)")
@app_commands.describe(message_id="Giveaway message ID (optional)")
async def gw_end(inter: discord.Interaction, message_id: Optional[str] = None):
    if not _gw_ok(inter):
        return await inter.response.send_message("You need Manage Server or the announcer role.", ephemeral=True)
    row = _gw_find(inter.guild_id, message_id)
    if not row:
        return await inter.response.send_message("No giveaway found.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    w = await end_giveaway(row)
    await inter.followup.send(f"✅ Ended. Winners: {', '.join(f'<@{x}>' for x in w) or 'none'}", ephemeral=True)

@gw_grp.command(name="reroll", description="Pick new winners (default: the latest giveaway)")
@app_commands.describe(message_id="Giveaway message ID (optional)")
async def gw_reroll(inter: discord.Interaction, message_id: Optional[str] = None):
    if not _gw_ok(inter):
        return await inter.response.send_message("You need Manage Server or the announcer role.", ephemeral=True)
    row = _gw_find(inter.guild_id, message_id)
    if not row:
        return await inter.response.send_message("No giveaway found.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    w = await end_giveaway(row, reroll=True)
    await inter.followup.send(f"🔁 New winners: {', '.join(f'<@{x}>' for x in w) or 'none'}", ephemeral=True)

tree.add_command(gw_grp)

@tree.command(name="poll", description="Create a poll")
@app_commands.guild_only()
@app_commands.describe(question="The question", options="Options separated by ; (2–10)",
                       hours="How long (default 24)", multiple="Allow choosing more than one")
async def poll_cmd(inter: discord.Interaction, question: app_commands.Range[str, 1, 300], options: str,
                   hours: app_commands.Range[int, 1, 768] = 24, multiple: bool = False):
    if not _gw_ok(inter):
        return await inter.response.send_message("You need Manage Server or the announcer role.", ephemeral=True)
    try:
        await make_poll(inter.channel, question, re.split(r"[;\n]", options), hours, multiple,
                        inter.user.display_name)
    except (ValueError, discord.HTTPException) as e:
        return await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
    await inter.response.send_message("📊 Poll posted!", ephemeral=True)
