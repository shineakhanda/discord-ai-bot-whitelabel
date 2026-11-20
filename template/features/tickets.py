"""Tickets: __BOT_NAME__ gives first-line AI support inside tickets created by any ticket bot."""
from typing import Literal

import discord
from discord import app_commands

import store
from core import client, tree, embed, is_owner, is_staff, cooled, prepare_text, log

async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

import re
ROLE_RE = re.compile(r"<@&(\d+)>")
USER_RE = re.compile(r"<@!?(\d+)>")

def _all_text(msg):
    """Message text plus embed text (many ticket bots put the pings inside embeds)."""
    parts = [msg.content or ""]
    for e in getattr(msg, "embeds", []) or []:
        parts += [e.title or "", e.description or ""]
        parts += [f"{f.name} {f.value}" for f in getattr(e, "fields", []) or []]
    return " ".join(parts)

async def maybe_open_ticket(msg):
    """Open a __BOT_NAME__ ticket when a ticket bot creates one. Works when:
    - the opening message pings a support role (in text OR embed), or
    - a ticket category is set and a bot posts the first message in a channel/thread inside it (no ping needed)."""
    gid = msg.guild.id
    g = store.guild(gid)
    t = g["tickets"]
    if not (t["enabled"] and g["features"]["tickets"]):
        return
    ch = msg.channel
    if store.ticket_get(ch.id):
        return
    text = _all_text(msg)
    pinged = {r.id for r in msg.role_mentions} | {int(x) for x in ROLE_RE.findall(text)}
    role_hit = bool(t["roles"]) and bool(pinged & set(t["roles"]))
    cat = getattr(ch, "category_id", None) or getattr(getattr(ch, "parent", None), "category_id", None)
    in_cat = bool(t["category"]) and cat == t["category"]
    if t["category"] and not in_cat:
        return
    if not msg.author.bot:
        return          # tickets are opened by the ticket bot's message, not by members pinging @support
    if not (role_hit or in_cat):
        return
    opener = next((u.id for u in msg.mentions if not u.bot), None)
    if opener is None:
        ids = [int(x) for x in USER_RE.findall(text)]
        opener = next((i for i in ids if i != client.user.id and i != msg.author.id), None)
    store.ticket_set(ch.id, gid, opener)
    who = f"<@{opener}> " if opener else ""
    e = embed(desc=f"👋 {who}I'm **__BOT_NAME__**, this server's AI assistant. Staff have been notified.\n"
                   "While you wait, tell me your issue (and any error or screenshot) and I'll try to help.\n"
                   "-# Staff: just send a message to take over, or use `/ticket ai off`.")
    try:
        await ch.send(embed=e)
    except discord.HTTPException as ex:
        log.warning("ticket greet failed in %s: %s (do I have access to the ticket channel?)", ch.id, ex)

async def ticket_message(msg, t):
    """Handle a human message in a known ticket. Returns True if handled."""
    gid = msg.guild.id
    if is_staff(msg.author, gid) and not is_owner(msg.author):
        if t["active"] and store.guild(gid)["tickets"]["handoff"]:
            store.ticket_active(msg.channel.id, False)
            try:
                await msg.channel.send(embed=embed(desc="🙋 Staff is here, I'll step back. "
                                                        "Mention me if you need me."))
            except discord.HTTPException:
                pass
        return client.user not in msg.mentions
    if not t["active"]:
        return False                                  # fall back to normal triggers (mention etc.)
    if t["opener"] and msg.author.id != t["opener"]:
        return client.user not in msg.mentions
    text = prepare_text(msg)
    if (not text and not msg.attachments) or not cooled(msg.author):
        return True
    from features.chat import notify_limit, respond
    if not store.quota(gid, msg.author.id, "chat"):
        await notify_limit(msg, "chat")
        return True
    await respond(msg, text, ticket=True)
    return True

# ---------- slash: tickets (staff) ----------
ticket_grp = app_commands.Group(name="ticket", description="__BOT_NAME__ in support tickets", guild_only=True)

@ticket_grp.command(name="ai", description="Turn __BOT_NAME__'s replies on/off in this ticket")
async def ticket_ai(inter: discord.Interaction, state: Literal["on", "off"]):
    if not is_staff(inter.user, inter.guild_id):
        return await _deny(inter, "Only ticket staff can do this.")
    t = store.ticket_get(inter.channel_id)
    if not t:
        if state == "off":
            return await _deny(inter, "I'm not active in this channel.")
        store.ticket_set(inter.channel_id, inter.guild_id, None)
    else:
        store.ticket_active(inter.channel_id, state == "on")
    await inter.response.send_message(f"🤖 __BOT_NAME__ AI in this ticket: **{state}**")

@ticket_grp.command(name="summary", description="AI summary of this ticket for staff")
async def ticket_summary(inter: discord.Interaction):
    if not is_staff(inter.user, inter.guild_id):
        return await _deny(inter, "Only ticket staff can do this.")
    if not store.quota(inter.guild_id, inter.user.id, "chat"):
        from core import limit_text
        return await _deny(inter, limit_text("chat"))
    await inter.response.defer(thinking=True, ephemeral=True)
    try:
        lines = []
        async for m in inter.channel.history(limit=100):
            txt = m.content or (m.embeds[0].description if m.embeds and m.embeds[0].description else "")
            if txt:
                lines.append(f"{m.author.display_name}{' (bot)' if m.author.bot else ''}: {txt[:300]}")
        convo = "\n".join(reversed(lines))[-7000:] or "(empty)"
        from agent import one_shot
        out = await one_shot("Summarize this support ticket for staff in short bullets: the user's problem, "
                             "details given, what was tried, and the next step needed. English.", convo, 450)
    except Exception as e:
        import ai
        out = ai.friendly_error(e, is_owner(inter.user))
    await inter.followup.send(f"🎫 **Ticket summary**\n{out}"[:1990], ephemeral=True)

tree.add_command(ticket_grp)
