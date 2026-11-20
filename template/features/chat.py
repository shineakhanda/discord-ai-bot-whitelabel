"""Chat: message routing, the respond flow, events, and everyday slash commands."""
import re, time, asyncio
from typing import Literal, Optional

import discord
from discord import app_commands

import store, ai, web, workspace, panel
from store import cfg, PREFIX, log
from core import (client, tree, embed, is_owner, is_admin, blocked, feature, cooled, chunks, limit_text,
                  prepare_text, on_startup, MAX_INPUT, EMOJI_RE)
import agent
from agent import Ctx, Progress, think, one_shot, SENSITIVE, Silent
from features import tickets, moderation
from timeparse import parse_when

_limit_notice = {}
NAME_RE = re.compile(r"(?<![\w])(__TRIGGERS__)(?![\w])", re.I)

async def notify_limit(msg, kind):
    key = (msg.guild.id if msg.guild else msg.author.id, kind, time.strftime("%Y-%m-%d"))
    if key in _limit_notice:
        return
    _limit_notice[key] = 1
    try:
        await msg.reply(limit_text(kind), mention_author=False)
    except discord.HTTPException:
        pass

# =====================================================================
# respond to a Discord message
# =====================================================================
async def respond(msg, text, ticket=False, auto=False):
    gid = msg.guild.id if msg.guild else None
    ref = msg.reference.resolved if msg.reference else None
    atts = list(msg.attachments)
    if isinstance(ref, discord.Message):
        if ref.author.id != client.user.id and ref.content:
            text = f"(replying to {ref.author.display_name}: {ref.content[:300]})\n{text}"
        if not atts and ref.attachments:
            atts = list(ref.attachments)
    prog = Progress(msg)
    ctx = Ctx(gid=gid, user=msg.author, channel=msg.channel, guild=msg.guild, message=msg, progress=prog,
              ticket=ticket, auto=auto)
    if gid:
        agent.add_person(ctx.people, msg.author)
        for u in msg.mentions:
            if u.id != client.user.id:
                agent.add_person(ctx.people, u)
        if isinstance(ref, discord.Message):
            agent.add_person(ctx.people, ref.author)
    try:
        async with msg.channel.typing():
            media = await agent.gather_media(atts) if atts else None
            if media and media[0]:
                text = (text + media[0]).strip()
            recent = None
            if gid and not auto and cfg["history_lines"] > 0:
                known = {c for _, r, c in ai.ctx.get(msg.channel.id, []) if r == "user"}
                try:
                    recent = await agent.channel_lines(msg.channel, cfg["history_lines"], before=msg, skip=known,
                                                       people=ctx.people)
                except discord.HTTPException:
                    recent = None
            out = await think(ctx, text, msg.channel.id, media=media, recent=recent)
            if cfg.get("human_delay") and prog.status is None:
                await asyncio.sleep(min(2.2, 0.5 + len(out) / 700))
    except Silent as s:
        await prog.cancel()
        if s.emoji:
            try:
                await msg.add_reaction(s.emoji)
            except discord.HTTPException:
                pass
        return
    except Exception as e:
        log.error("AI error: %s", e)
        out = ai.friendly_error(e, is_owner(msg.author))
    await prog.finish(out, ctx.mentioned)
    await agent.flush_after(ctx)

# =====================================================================
# routing
# =====================================================================
@client.event
async def on_message(msg: discord.Message):
    if msg.author.id == client.user.id:
        return
    gid = msg.guild.id if msg.guild else None
    if gid and msg.author.bot:
        await tickets.maybe_open_ticket(msg)
    if msg.author.bot or blocked(msg.author, gid):
        return
    content = msg.content.strip()

    if content.startswith(PREFIX) and is_owner(msg.author):
        from features import owner
        parts = content[len(PREFIX):].split(maxsplit=1)
        if parts and parts[0].lower() in owner.OWNER_CMDS:
            try:
                return await owner.owner_cmd(msg, parts[0].lower(), parts[1].strip() if len(parts) > 1 else "")
            except Exception as e:
                return await msg.channel.send(f"⚠️ {e}"[:1990])

    if cfg["paused"] and not is_owner(msg.author):
        return

    if gid:
        try:
            if await moderation.check_message(msg):
                return
        except Exception as e:
            log.warning("moderation check failed: %s", e)
        try:
            from features import activities
            if await activities.handle_message(msg):    # counting / word chain / guess channels
                return
        except Exception as e:
            log.warning("activities failed: %s", e)
        t = store.ticket_get(msg.channel.id)
        if t and await tickets.ticket_message(msg, t):
            return

    mentioned = client.user in msg.mentions
    ref = msg.reference.resolved if msg.reference else None
    replied = isinstance(ref, discord.Message) and ref.author.id == client.user.id
    auto = gid is not None and msg.channel.id in store.guild(gid)["auto_channels"]
    called = gid is not None and cfg.get("name_trigger", True) and bool(NAME_RE.search(content))
    mentioned = mentioned or called                      # "__bot_slug__, eta ki?" works like a mention
    if not (gid is None or mentioned or replied or auto):
        return
    if content.startswith(PREFIX) and not mentioned:
        return
    if not feature(gid, "chat") or not cooled(msg.author):
        return
    text = prepare_text(msg)
    has_media = bool(msg.attachments) or (isinstance(ref, discord.Message) and bool(ref.attachments))
    if not text and not has_media:
        if mentioned:
            await msg.reply("Hi! Ki obostha? 😄 Ask me anything, or try `/help`.", mention_author=False)
        return
    if not store.quota(gid, msg.author.id, "chat"):
        return await notify_limit(msg, "chat")
    await respond(msg, text, auto=auto and not mentioned and not replied)

# =====================================================================
# events and background loops
# =====================================================================
@on_startup
async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        ai.prune_ctx()
        store.prune_usage()
        workspace.prune_notes()
        if store.last_backup_age() > 86400:              # daily database backup
            try:
                store.backup_db("daily")
            except Exception as e:
                log.warning("daily backup failed: %s", e)
        today = time.strftime("%Y-%m-%d")
        for k in [k for k in _limit_notice if k[2] != today]:
            del _limit_notice[k]

@on_startup
async def reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for rid, uid, cid, gid, text in store.rem_due():
                store.rem_done(rid)
                am = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(uid)])
                ch = client.get_channel(cid) if cid else None
                try:
                    if ch is None:
                        raise LookupError("channel gone")
                    await ch.send(f"⏰ <@{uid}> reminder: {text}", allowed_mentions=am)
                except Exception:
                    try:
                        u = await client.fetch_user(uid)
                        await u.send(f"⏰ Reminder: {text}")
                    except Exception as e:
                        log.warning("reminder %s undeliverable: %s", rid, e)
        except Exception as e:
            log.error("reminder loop: %s", e)
        await asyncio.sleep(20)

@on_startup
async def lightpanda_install():
    await web.ensure_lightpanda()

async def clean_old_commands():
    """Remove slash commands left on this application by a previous bot (e.g. Hermes agent / OpenClaw)."""
    removed = 0
    for g in client.guilds:
        try:
            old = await tree.fetch_commands(guild=g)
            if old:
                tree.clear_commands(guild=g)
                await tree.sync(guild=g)
                removed += len(old)
        except discord.HTTPException as e:
            log.warning("clean commands in %s: %s", g.id, e)
        await asyncio.sleep(0.5)
    synced = await tree.sync()
    return removed, len(synced)

_started = {"done": False}

@client.event
async def on_ready():
    log.info("Logged in as %s | %d servers | model=%s | browser=%s | search=%s | skills=%s",
             client.user, len(client.guilds), cfg["model"], web.browser_name(), web.active_engines(),
             list(workspace.skills()))
    from features import presence
    await presence.apply()
    if not _started["done"]:
        _started["done"] = True
        from features import serverlog
        asyncio.create_task(serverlog.snapshot())
        if store.env("CLEAN_OLD_COMMANDS") == "1":
            removed, n = await clean_old_commands()
            log.info("cleaned %d old server commands, %d global commands active", removed, n)

@client.event
async def on_guild_join(g):
    if g.id in cfg["blocked_guilds"]:
        return await g.leave()
    from features import serverlog
    asyncio.create_task(serverlog.on_join(g))
    ch = g.system_channel
    if ch and ch.permissions_for(g.me).send_messages:
        e = embed("Assalamu Alaikum! I'm __BOT_NAME__ 👋",
                  "Your new AI member: chat in English, বাংলা or Banglish, search, read links, see photos, "
                  "help in tickets, manage game panels, write announcements and keep chat clean.\n"
                  "• Mention me or use `/ask` · `/help` for everything\n"
                  "• Admins: `/setup view` to configure me")
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

@client.event
async def on_guild_remove(g):
    from features import serverlog
    asyncio.create_task(serverlog.on_leave(g))
    store.delete_guild(g.id)
    panel.clear_cache(g.id)

@client.event
async def on_guild_channel_delete(ch):
    store.ticket_delete(ch.id)
    ai.forget(ch.id)
    g = store.guild(ch.guild.id)
    if ch.id in g["auto_channels"]:
        g["auto_channels"].remove(ch.id); store.save_guild(ch.guild.id)

@client.event
async def on_thread_delete(th):
    store.ticket_delete(th.id)
    ai.forget(th.id)

# =====================================================================
# slash commands
# =====================================================================
HELP = (
    "**Chat** — mention me, reply to me, DM me, or `/ask`. English, বাংলা, Banglish and more.\n"
    "**Search & links** — I search the web and open links by myself. `/search`, `/read`.\n"
    "**Photos, voice, files** — send them with your question (crash logs welcome).\n"
    "**Context & memory** — I read recent chat. Say *\"remember that I play Minecraft\"* · `/memory` · `/profile`.\n"
    "**Reminders** — *\"remind me in 2h…\"* or `/remind`, list with `/reminders`.\n"
    "**Catch up / translate** — `/summarize`, `/translate`, right-click a message → Apps.\n"
    "**Announcements** — admins: *\"announce our event on friday 8pm\"* or `/announce`, `/schedule`.\n"
    "**Game panel** — `/panel`, or ask *\"why won't SMP start?\"* (admins: I can fix it).\n"
    "**Moderation** — I warn rule breakers like a fair mod and report to staff. `/warn`, `/warnings`, right-click → Report.\n"
    "**Tickets** — I help inside tickets from your ticket bot.\n"
    "**Activities** — `/activity` QOTD, trivia, counting & word games · `/giveaway` · `/poll` · `/leaderboard`.\n"
    "**Web data** — `/extract` a site (React/JS too) into a table + CSV · `/watch` pages for price/stock changes.\n"
    "**VPS help** — `/vps connect` (tmate or IP) and I'll check & fix it · admins: `/ssh`.\n"
    "**Admins** — `/setup type` (community, store, support, Minecraft SMP…) · `/setup view` for every setting.\n"
    "-# Privacy: chat memory stays in RAM for 2 hours. Saved facts only when you ask; delete with `/memory clear`."
)

async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

async def _gate(inter, kind="chat"):
    gid = inter.guild_id
    if blocked(inter.user, gid) or (cfg["paused"] and not is_owner(inter.user)):
        await _deny(inter, "I'm not available right now."); return False
    if not feature(gid, kind):
        await _deny(inter, f"`{kind}` is turned off on this server."); return False
    if not cooled(inter.user):
        await _deny(inter, "⏳ Slow down a little, try again in a few seconds."); return False
    if not store.quota(gid, inter.user.id, kind):
        await _deny(inter, limit_text(kind)); return False
    return True

async def _send_long(inter, text, ephemeral=False):
    parts = chunks(text)
    try:
        await inter.edit_original_response(content=parts[0])
    except discord.HTTPException:
        await inter.followup.send(parts[0], ephemeral=ephemeral)
    for p in parts[1:]:
        await inter.followup.send(p, ephemeral=ephemeral)

def _ctx(inter):
    c = Ctx(gid=inter.guild_id, user=inter.user, channel=inter.channel, guild=inter.guild,
            progress=slash_progress(inter))
    _pending[inter.id] = c
    return c

_pending = {}

async def _finish(inter):
    c = _pending.pop(inter.id, None)
    if c:
        await agent.flush_after(c)

def slash_progress(inter):
    async def prog(text):
        try:
            await inter.edit_original_response(content=text)
        except discord.HTTPException:
            pass
    return prog

@tree.command(name="help", description="What __BOT_NAME__ can do")
async def help_cmd(inter: discord.Interaction):
    e = embed("__BOT_NAME__ 🤖 — owned by __OWNER__", HELP)
    await inter.response.send_message(embed=e, ephemeral=True)

@tree.command(name="ask", description="Ask __BOT_NAME__ anything")
@app_commands.describe(prompt="Your question (any language, Banglish works too)")
async def ask_cmd(inter: discord.Interaction, prompt: app_commands.Range[str, 1, MAX_INPUT]):
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True)
    try:
        out = await think(_ctx(inter), prompt, inter.channel_id)
    except Exception as e:
        out = ai.friendly_error(e, is_owner(inter.user))
    await _send_long(inter, f"> {prompt[:200]}\n{out}")
    await _finish(inter)

@tree.command(name="search", description="Search the web and get a short answer")
async def search_cmd(inter: discord.Interaction, query: app_commands.Range[str, 1, 200]):
    if not web.search_ready():
        return await _deny(inter, "🔎 Web search isn't available right now.")
    if not await _gate(inter, "chat"):
        return
    await inter.response.defer(thinking=True)
    try:
        out = await think(_ctx(inter), query, inter.channel_id, force_query=query)
    except Exception as e:
        out = ai.friendly_error(e, is_owner(inter.user))
    await _send_long(inter, f"🔎 **{query[:150]}**\n{out}")
    await _finish(inter)

@tree.command(name="read", description="Open a web page and summarize it or answer about it")
@app_commands.describe(url="The link", question="Optional: what do you want to know about it?")
async def read_cmd(inter: discord.Interaction, url: str, question: Optional[app_commands.Range[str, 1, 500]] = None):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not await _gate(inter, "chat"):
        return
    await inter.response.defer(thinking=True)
    text = f"{question or 'Summarize this page in a few bullet points.'} {url}"
    try:
        out = await think(_ctx(inter), text, inter.channel_id)
    except Exception as e:
        out = ai.friendly_error(e, is_owner(inter.user))
    await _send_long(inter, f"🌐 <{url}>\n{out}")
    await _finish(inter)

@tree.command(name="summarize", description="Catch up: summarize recent messages in this channel")
@app_commands.describe(count="How many recent messages (10-100)")
async def summarize_cmd(inter: discord.Interaction, count: app_commands.Range[int, 10, 100] = 50):
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True, ephemeral=True)
    try:
        lines = []
        async for m in inter.channel.history(limit=count):
            if m.content and m.author.id != client.user.id:
                body = EMOJI_RE.sub(r"\1", m.content)[:200]
                lines.append(f"{m.author.display_name}: {body}")
        if not lines:
            return await inter.followup.send("Nothing to summarize here.", ephemeral=True)
        convo = "\n".join(reversed(lines))[-6000:]
        out = await one_shot(
            "You are __BOT_NAME__ (owned by __OWNER__). Summarize this Discord conversation in short bullet points: "
            "main topics, decisions, open questions, and who asked what. Use the language most used in the chat.",
            convo, 500)
    except discord.Forbidden:
        out = "I need **Read Message History** permission in this channel."
    except Exception as e:
        out = ai.friendly_error(e, is_owner(inter.user))
    await _send_long(inter, f"📝 **Last {count} messages**\n{out}", ephemeral=True)

TRANSLATE_AUTO = ("If the text is Bangla or Banglish, translate it to English. "
                  "Otherwise translate it to Bangla (Bengali script).")

@tree.command(name="translate", description="Translate text (Bangla ⇄ English by default)")
@app_commands.describe(text="Text to translate", to="Target language, e.g. English, Bangla, Banglish, Hindi")
async def translate_cmd(inter: discord.Interaction, text: app_commands.Range[str, 1, MAX_INPUT],
                        to: Optional[app_commands.Range[str, 1, 30]] = None):
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True)
    try:
        out = await agent.translate(text, to)
    except Exception as e:
        out = ai.friendly_error(e, is_owner(inter.user))
    await _send_long(inter, f"> {text[:300]}\n{out}")

@tree.context_menu(name="Translate (__BOT_NAME__)")
async def translate_ctx(inter: discord.Interaction, message: discord.Message):
    if not message.content:
        return await _deny(inter, "That message has no text.")
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True, ephemeral=True)
    try:
        out = await agent.translate(message.content[:MAX_INPUT])
    except Exception as e:
        out = ai.friendly_error(e, is_owner(inter.user))
    await _send_long(inter, out, ephemeral=True)

@tree.context_menu(name="Explain (__BOT_NAME__)")
async def explain_ctx(inter: discord.Interaction, message: discord.Message):
    if not message.content:
        return await _deny(inter, "That message has no text.")
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True, ephemeral=True)
    try:
        out = await one_shot("You are __BOT_NAME__ (owned by __OWNER__). Explain this Discord message simply and briefly "
                             "(slang, code, jokes, context) in the language it is written in.",
                             message.content[:MAX_INPUT])
    except Exception as e:
        out = ai.friendly_error(e, is_owner(inter.user))
    await _send_long(inter, out, ephemeral=True)

@tree.command(name="forget", description="Clear __BOT_NAME__'s chat memory in this channel")
async def forget_cmd(inter: discord.Interaction):
    ok = inter.guild is None or is_owner(inter.user) or inter.channel.permissions_for(inter.user).manage_messages
    if not ok:
        return await _deny(inter, "Only mods (Manage Messages) can clear my memory here.")
    ai.forget(inter.channel_id)
    await inter.response.send_message("🧹 Memory cleared for this channel.")

memory_grp = app_commands.Group(name="memory", description="What __BOT_NAME__ remembers about you")

@memory_grp.command(name="view", description="See what __BOT_NAME__ remembers about you")
async def memory_view(inter: discord.Interaction):
    rows = store.mem_list(inter.user.id)
    body = "\n".join(f"`{i}.` {t}" for i, (_, t) in enumerate(rows, 1)) or "Nothing yet. Say *\"remember that …\"*."
    await inter.response.send_message(embed=embed("🧠 Your memory", body), ephemeral=True)

@memory_grp.command(name="add", description="Tell __BOT_NAME__ something to remember about you")
async def memory_add(inter: discord.Interaction, fact: app_commands.Range[str, 1, 200]):
    if SENSITIVE.search(fact):
        return await _deny(inter, "That looks like sensitive data (passwords, keys, numbers, emails), so I won't save it.")
    await inter.response.send_message(f"🧠 {store.mem_add(inter.user.id, fact).capitalize()}: {fact}", ephemeral=True)

@memory_grp.command(name="forget", description="Delete one saved fact (number from /memory view, or words)")
async def memory_forget(inter: discord.Interaction, which: app_commands.Range[str, 1, 100]):
    n = store.mem_forget(inter.user.id, which)
    await inter.response.send_message(f"🧹 Deleted {n} fact(s)." if n else "Nothing matched.", ephemeral=True)

@memory_grp.command(name="clear", description="Delete everything __BOT_NAME__ remembers about you")
async def memory_clear(inter: discord.Interaction, confirm: Literal["yes"]):
    store.mem_clear(inter.user.id)
    await inter.response.send_message("🧹 All your saved memories are deleted.", ephemeral=True)

tree.add_command(memory_grp)

@tree.command(name="remind", description="Set a reminder")
@app_commands.describe(when="e.g. 10m, 2h, 8pm, tomorrow 9am, friday 20:00", text="What to remind you about")
async def remind_cmd(inter: discord.Interaction, when: app_commands.Range[str, 1, 40],
                     text: app_commands.Range[str, 1, 300]):
    tz = store.guild(inter.guild_id)["timezone"] if inter.guild_id else "Asia/Dhaka"
    due = parse_when(when, tz)
    if not due:
        return await _deny(inter, "Use a time like `10m`, `2h`, `8pm`, `tomorrow 9am` or `friday 20:00`.")
    due = int(max(time.time() + 60, min(due, time.time() + 30 * 86400)))
    rid = store.rem_add(inter.user.id, inter.channel_id, inter.guild_id, due, text)
    if not rid:
        return await _deny(inter, f"You already have {store.MAX_REMINDERS} reminders. Cancel one with `/reminders`.")
    await inter.response.send_message(f"⏰ Reminder #{rid} set <t:{due}:R>: {text}", ephemeral=True)

@tree.command(name="reminders", description="Your reminders (optionally cancel one)")
@app_commands.describe(cancel="Reminder number to cancel")
async def reminders_cmd(inter: discord.Interaction, cancel: Optional[int] = None):
    if cancel is not None:
        ok = store.rem_cancel(inter.user.id, cancel)
        return await inter.response.send_message("🗑️ Cancelled." if ok else "No reminder with that number.",
                                                 ephemeral=True)
    rows = store.rem_list(inter.user.id)
    body = "\n".join(f"`#{i}` <t:{int(d)}:R> — {t}" for i, d, t in rows) or "No reminders. Try `/remind`."
    await inter.response.send_message(embed=embed("⏰ Your reminders", body), ephemeral=True)


@tree.command(name="profile", description="How __BOT_NAME__ should call you and answer you")
@app_commands.describe(nickname="What __BOT_NAME__ calls you", language="e.g. Banglish, Bangla, English",
                       style="Answer length you like", clear="Reset your profile")
async def profile_cmd(inter: discord.Interaction, nickname: Optional[app_commands.Range[str, 1, 40]] = None,
                      language: Optional[app_commands.Range[str, 2, 30]] = None,
                      style: Optional[Literal["short", "normal", "detailed"]] = None, clear: bool = False):
    if clear:
        store.profile_clear(inter.user.id)
        return await inter.response.send_message("🪪 Profile reset.", ephemeral=True)
    if nickname or language or style:
        kw = {k: v for k, v in (("nickname", nickname), ("language", language), ("style", style)) if v}
        store.profile_set(inter.user.id, **kw)
    p = store.profile(inter.user.id)
    body = "\n".join(f"**{k}**: {v}" for k, v in p.items()) or "Empty. Set a nickname, language or style."
    await inter.response.send_message(embed=embed("🪪 Your profile", body), ephemeral=True)
