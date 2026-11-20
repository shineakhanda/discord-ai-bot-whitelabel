"""Announcements (AI drafts → approve → post like a person) and scheduled / repeating messages."""
import re, json, time, asyncio
from typing import Optional, Literal

import discord
from discord import app_commands

import store, workspace
from store import log
from core import (client, tree, embed, can_announce, feature, find_channel, ping_mentions, safe_send, on_startup,
                  limit_text, chunks)
from agent import tool, Result, one_shot, split_args
from timeparse import parse_when, parse_repeat, fmt_repeat

DRAFT_SYSTEM = """You write Discord announcements for a community, as its friendly community manager (you are __BOT_NAME__).
Write {n} different drafts. Each: a hook line with one emoji, the key facts (what, when incl. timezone, where, how to join / prize),
and one call to action. Natural and human, scannable, max ~120 words, max 4 emoji, no hashtags, no @mentions.
Language: {lang}.
Return ONLY JSON: {{"drafts": ["draft 1", "draft 2", "draft 3"]}}"""

LANG_NAMES = {"bn": "Bangla script", "banglish": "Banglish (Bangla in English letters)", "en": "English"}

def _lang(gid, details):
    mode = store.guild(gid)["lang"]
    if mode in LANG_NAMES:
        return LANG_NAMES[mode]
    from workspace import lang_hint
    h = lang_hint(details)
    return "Bangla script" if "script" in h else "Banglish" if "Banglish" in h else "the same language as the details"

async def make_drafts(gid, details, n=3):
    raw = await one_shot(DRAFT_SYSTEM.format(n=n, lang=_lang(gid, details)),
                         f"Server: {client.get_guild(gid).name if client.get_guild(gid) else ''}\nDetails: {details}", 900)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        drafts = [d.strip() for d in json.loads(m.group())["drafts"] if d.strip()]
    except Exception:
        drafts = [p.strip() for p in re.split(r"\n\s*(?:---+|Draft \d+:?)\s*\n", raw) if p.strip()]
    return drafts[:n] or [raw.strip()]

async def post(channel, text, ping, actor):
    prefix, am = ping_mentions(ping, channel.guild, actor)
    msg = None
    for i, part in enumerate(chunks(prefix + text)):
        msg = await channel.send(part, allowed_mentions=am if i == 0 else discord.AllowedMentions.none()) or msg
    return msg

class DraftView(discord.ui.View):
    """Shows drafts; the requester picks one to post (or schedule)."""
    def __init__(self, gid, author_id, channel_id, ping, details, drafts, when=None):
        super().__init__(timeout=3600)
        self.gid, self.author_id, self.channel_id, self.ping = gid, author_id, channel_id, ping
        self.details, self.drafts, self.when = details, drafts, when
        for i in range(len(drafts)):
            b = discord.ui.Button(label=f"{'Schedule' if when else 'Post'} {i + 1}", style=discord.ButtonStyle.success,
                                  emoji="📅" if when else "📢")
            b.callback = self._pick(i)
            self.add_item(b)
        r = discord.ui.Button(label="New drafts", style=discord.ButtonStyle.secondary, emoji="🔄")
        r.callback = self._redo
        self.add_item(r)
        x = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
        x.callback = self._cancel
        self.add_item(x)

    def text(self):
        ch = f"<#{self.channel_id}>"
        head = f"📝 **Announcement drafts** for {ch}" + (f" · ping: `{self.ping}`" if self.ping not in ("", "none") else "") \
            + (f" · scheduled <t:{int(self.when)}:F>" if self.when else "")
        body = "\n\n".join(f"**{i + 1}.**\n{d}" for i, d in enumerate(self.drafts))
        return (head + "\n\n" + body + "\n\n-# Pick one below. Only the person who asked can choose.")[:1990]

    async def interaction_check(self, inter):
        if inter.user.id != self.author_id:
            await inter.response.send_message("Only the person who asked can choose.", ephemeral=True)
            return False
        return True

    def _pick(self, i):
        async def cb(inter: discord.Interaction):
            ch = client.get_channel(self.channel_id)
            if not ch:
                return await inter.response.send_message("That channel is gone.", ephemeral=True)
            text = self.drafts[i]
            if self.when:
                sid = store.sched_add(self.gid, ch.id, text, self.when, 0, self.ping, inter.user.id)
                note = f"📅 Scheduled #{sid} for <t:{int(self.when)}:F> in {ch.mention}." if sid else \
                    f"Too many schedules (max {store.MAX_SCHEDULES})."
            else:
                msg = await post(ch, text, self.ping, inter.user)
                note = f"📢 Posted in {ch.mention}" + (f" → {msg.jump_url}" if msg else "")
            workspace.note(f"guild {self.gid}: announcement by {inter.user} in #{ch.name}: {text[:80]}")
            self.stop()
            await inter.response.edit_message(content=f"{note}\n\n> {text[:1500]}", view=None)
        return cb

    async def _redo(self, inter: discord.Interaction):
        if not store.quota(self.gid, inter.user.id, "announce"):
            return await inter.response.send_message(limit_text("announce"), ephemeral=True)
        await inter.response.defer()
        try:
            self.drafts = await make_drafts(self.gid, self.details)
        except Exception as e:
            return await inter.followup.send(f"⚠️ {e}", ephemeral=True)
        await inter.edit_original_response(content=self.text(), view=self)

    async def _cancel(self, inter: discord.Interaction):
        self.stop()
        await inter.response.edit_message(content="✖️ Announcement cancelled.", view=None)

# ---------- AI tools ----------
def _ok(c):
    return bool(c.gid and c.guild and can_announce(c.user, c.gid))

def _target(c, text):
    default = store.guild(c.gid)["announce"]["channel"]
    ch = find_channel(c.guild, text) if text else None
    return ch or (c.guild.get_channel(default) if default else None) or c.channel

@tool("announce_drafts", "announce_drafts | <#channel> | <ping: none/here/everyone/@role> | <details> | <when, optional>",
      "write 3 announcement drafts; the admin picks one with buttons and it gets posted (or scheduled)",
      when=_ok, progress="📝 Writing announcement drafts…")
async def t_drafts(c, a):
    a = (a + ["", "", "", ""])[:4] if len(a) < 4 else a[:3] + [" | ".join(a[3:])]
    ch = _target(c, a[0])
    details, when_s = a[2] or a[0], a[3].strip()
    if not store.quota(c.gid, c.user.id, "announce"):
        return Result("Today's announcement limit for this server is used up.")
    when = None
    if when_s:
        when = parse_when(when_s, store.guild(c.gid)["timezone"])
        if not when:
            return Result(f"Couldn't understand the time '{when_s}'. Use e.g. 'friday 8pm' or '2026-12-01 20:00'.")
    drafts = await make_drafts(c.gid, details)
    view = DraftView(c.gid, c.user.id, ch.id, a[1] or "none", details, drafts, when)
    c.after_send.append((view.text(), view, {}))
    return Result(f"{len(drafts)} drafts are shown below your reply with buttons. Tell the user to pick one "
                  f"(it posts in #{ch.name}{' at the scheduled time' if when else ''}). Don't repeat the drafts.",
                  "📝 drafted announcement")

@tool("announce_post", "announce_post | <#channel> | <ping> | <exact text>",
      "post exactly this announcement now (when the admin gave the final text)", when=_ok,
      progress="📢 Posting announcement…")
async def t_post(c, a):
    a = split_args(" | ".join(a), 3)
    if len(a) < 3 or not a[2]:
        return Result("Format: announce_post | #channel | ping | text")
    ch = _target(c, a[0])
    msg = await post(ch, a[2].replace("\\n", "\n"), a[1], c.user)
    workspace.note(f"guild {c.gid}: announcement by {c.user} in #{ch.name}: {a[2][:80]}")
    return Result(f"Posted in #{ch.name}" + (f": {msg.jump_url}" if msg else " (failed: check my permissions)."),
                  f"📢 posted in #{ch.name}")

@tool("schedule_add", "schedule_add | <#channel> | <when> | <repeat: none/daily/weekly/every 6h> | <ping> | <text>",
      "schedule a message (once or repeating)", when=_ok, progress="📅 Scheduling…")
async def t_sched(c, a):
    a = split_args(" | ".join(a), 5)
    if len(a) < 5 or not a[4]:
        return Result("Format: schedule_add | #channel | when | repeat | ping | text")
    ch = _target(c, a[0])
    from core import has_perm
    if a[3].strip().lower() in ("everyone", "@everyone", "here", "@here") and not has_perm(c.user, "mention_everyone"):
        a[3] = "none"
    when = parse_when(a[1], store.guild(c.gid)["timezone"])
    every = parse_repeat(a[2])
    if not when:
        return Result(f"Couldn't understand the time '{a[1]}'.")
    if every is None:
        return Result("Repeat must be none, hourly, daily, weekly, or 'every Nh' (min 1h).")
    sid = store.sched_add(c.gid, ch.id, a[4].replace("\\n", "\n"), when, every, a[3] or "none", c.user.id)
    if not sid:
        return Result(f"Too many schedules on this server (max {store.MAX_SCHEDULES}).")
    workspace.note(f"guild {c.gid}: schedule #{sid} by {c.user} in #{ch.name} ({fmt_repeat(every)})")
    return Result(f"Scheduled #{sid} in #{ch.name}, first at <t:{int(when)}:F>, {fmt_repeat(every)}.", "📅 scheduled")

@tool("schedule_list", "schedule_list", "list scheduled messages on this server", when=_ok,
      progress="📅 Checking schedules…")
async def t_slist(c, a):
    rows = store.sched_list(c.gid)
    return Result("; ".join(f"#{i} in <#{ch}> next <t:{int(n)}:R> {fmt_repeat(e)}: {t[:60]}" for i, ch, t, n, e, _ in rows)
                  or "No scheduled messages.", "📅 listed schedules")

@tool("schedule_cancel", "schedule_cancel | <id>", "cancel a scheduled message", when=_ok,
      progress="📅 Cancelling schedule…")
async def t_scancel(c, a):
    sid = int(re.sub(r"\D", "", a[0]) or 0)
    return Result("Cancelled." if store.sched_cancel(c.gid, sid) else "No schedule with that id.", "📅 cancelled")

# ---------- slash commands ----------
async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

@tree.command(name="announce", description="__BOT_NAME__ writes announcement drafts; you pick one to post or schedule")
@app_commands.guild_only()
@app_commands.describe(details="What to announce (event, time, prize, link…)", channel="Where to post",
                       ping="Who to ping", when="Optional: schedule it, e.g. 'friday 8pm', 'tomorrow 9am'")
async def announce_cmd(inter: discord.Interaction, details: app_commands.Range[str, 5, 1500],
                       channel: Optional[discord.TextChannel] = None,
                       ping: Literal["none", "here", "everyone"] = "none", role: Optional[discord.Role] = None,
                       when: Optional[str] = None):
    if not can_announce(inter.user, inter.guild_id):
        return await _deny(inter, "You need Manage Server (or the announcer role) to announce.")
    if not store.quota(inter.guild_id, inter.user.id, "announce"):
        return await _deny(inter, limit_text("announce"))
    ts = None
    if when:
        ts = parse_when(when, store.guild(inter.guild_id)["timezone"])
        if not ts:
            return await _deny(inter, "Couldn't understand the time. Try `friday 8pm` or `2026-12-01 20:00`.")
    await inter.response.defer(thinking=True, ephemeral=True)
    ch = channel or _default(inter)
    try:
        drafts = await make_drafts(inter.guild_id, details)
    except Exception as e:
        return await inter.followup.send(f"⚠️ {e}", ephemeral=True)
    view = DraftView(inter.guild_id, inter.user.id, ch.id, role.mention if role else ping, details, drafts, ts)
    await inter.followup.send(view.text(), view=view, ephemeral=True)

def _default(inter):
    d = store.guild(inter.guild_id)["announce"]["channel"]
    return (inter.guild.get_channel(d) if d else None) or inter.channel

sched_grp = app_commands.Group(name="schedule", description="Scheduled and repeating messages", guild_only=True)

@sched_grp.command(name="add", description="Schedule a message (once or repeating)")
@app_commands.describe(when="e.g. 2h, 8pm, tomorrow 9am, friday 20:00, 2026-12-01 18:30",
                       repeat="none, hourly, daily, weekly, or 'every 6h'", text="Message text (use \\n for new lines)")
async def sched_add(inter: discord.Interaction, channel: discord.TextChannel, when: str,
                    text: app_commands.Range[str, 1, 1900], repeat: str = "none",
                    ping: Literal["none", "here", "everyone"] = "none", role: Optional[discord.Role] = None):
    if not can_announce(inter.user, inter.guild_id):
        return await _deny(inter, "You need Manage Server (or the announcer role).")
    ts = parse_when(when, store.guild(inter.guild_id)["timezone"])
    every = parse_repeat(repeat)
    if not ts:
        return await _deny(inter, "Couldn't understand the time.")
    if every is None:
        return await _deny(inter, "Repeat must be none, hourly, daily, weekly, or `every 6h` (min 1h).")
    if ping != "none" and not inter.user.guild_permissions.mention_everyone:
        ping = "none"
    sid = store.sched_add(inter.guild_id, channel.id, text.replace("\\n", "\n"), ts, every,
                          role.mention if role else ping, inter.user.id)
    if not sid:
        return await _deny(inter, f"Max {store.MAX_SCHEDULES} schedules per server.")
    await inter.response.send_message(f"📅 #{sid} → {channel.mention} first <t:{int(ts)}:F> · {fmt_repeat(every)}",
                                      ephemeral=True)

@sched_grp.command(name="list", description="List scheduled messages")
async def sched_list_cmd(inter: discord.Interaction):
    rows = store.sched_list(inter.guild_id)
    body = "\n".join(f"`#{i}` <#{ch}> · <t:{int(n)}:R> · {fmt_repeat(e)} — {t[:70]}" for i, ch, t, n, e, _ in rows)
    await inter.response.send_message(embed=embed("📅 Scheduled messages", body or "None yet."), ephemeral=True)

@sched_grp.command(name="remove", description="Cancel a scheduled message")
async def sched_remove(inter: discord.Interaction, id: int):
    if not can_announce(inter.user, inter.guild_id):
        return await _deny(inter, "You need Manage Server (or the announcer role).")
    ok = store.sched_cancel(inter.guild_id, id)
    await inter.response.send_message("🗑️ Cancelled." if ok else "No schedule with that id.", ephemeral=True)

tree.add_command(sched_grp)

# ---------- scheduler loop (the heartbeat) ----------
@on_startup
async def schedule_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for sid, gid, cid, text, every, ping, by_id in store.sched_due():
                store.sched_done(sid, every)
                ch = client.get_channel(cid)
                if ch is not None and gid not in store.cfg["blocked_guilds"]:
                    await post(ch, text, ping, None)
        except Exception as e:
            log.error("schedule loop: %s", e)
        await asyncio.sleep(30)
