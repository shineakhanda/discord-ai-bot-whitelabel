"""Staff tools: saved replies, private staff notes on members, member lookup, hand-off to a human, conversation
summaries, and extra moderation actions (roles, nicknames, unban, softban)."""
import re, time
from datetime import datetime, timezone

import discord
from discord import app_commands

import store
from store import db, log
from core import client, tree, is_staff, has_perm, find_member, first_id, safe_send
from agent import tool, Result, one_shot, channel_lines
from features.moderation import staff_ok, _target, _need, _mod, report

db.executescript("""
CREATE TABLE IF NOT EXISTS saved_replies (guild_id INTEGER, name TEXT, body TEXT, by_id INTEGER, uses INTEGER DEFAULT 0,
                                          created REAL, PRIMARY KEY (guild_id, name));
CREATE TABLE IF NOT EXISTS staff_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
                                        note TEXT, by_id INTEGER, ts REAL);
""")
db.commit()

def _slug(name):
    return re.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-")[:40]

# ---------------------------------------------------------------------------------------------------------------
# saved replies (canned answers staff reuse: payment details, delivery time, refund policy…)
# ---------------------------------------------------------------------------------------------------------------
def replies(gid):
    return db.execute("SELECT name, body, uses FROM saved_replies WHERE guild_id=? ORDER BY uses DESC, name",
                      (gid,)).fetchall()

def reply_get(gid, name):
    row = db.execute("SELECT body FROM saved_replies WHERE guild_id=? AND name=?", (gid, _slug(name))).fetchone()
    if row:
        db.execute("UPDATE saved_replies SET uses=uses+1 WHERE guild_id=? AND name=?", (gid, _slug(name)))
        db.commit()
    return row[0] if row else None

@tool("reply_save", "reply_save | <short name, e.g. payment> | <full reply text>",
      "(staff) save a canned reply the team (and you) can reuse", when=staff_ok, progress="💾 Saving reply…")
async def t_reply_save(c, a):
    if len(a) < 2 or not _slug(a[0]) or not a[1].strip():
        return Result("Give a name and the reply text.")
    db.execute("INSERT OR REPLACE INTO saved_replies (guild_id, name, body, by_id, uses, created) VALUES (?,?,?,?,0,?)",
               (c.gid, _slug(a[0]), " | ".join(a[1:]).strip()[:1900], c.user.id, time.time()))
    db.commit()
    return Result(f"Saved reply **{_slug(a[0])}**. Use it with /reply or ask me for it.", f"💾 saved reply {_slug(a[0])}")

@tool("saved_reply", "saved_reply | <name, or 'list'>",
      "get one of this server's saved staff replies (payment details, delivery, refund policy…) to answer with it "
      "exactly; 'list' shows all names", when=lambda c: bool(c.gid), progress="📋 Getting saved reply…")
async def t_reply(c, a):
    name = (a[0] if a else "list").strip()
    if not name or name.lower() == "list":
        rows = replies(c.gid)
        return Result("Saved replies: " + (", ".join(r[0] for r in rows) or "none yet (staff can add with reply_save)"))
    body = reply_get(c.gid, name)
    if body is None:
        rows = [r[0] for r in replies(c.gid) if _slug(name) in r[0] or r[0] in _slug(name)]
        if len(rows) == 1:
            body = reply_get(c.gid, rows[0])
        else:
            return Result(f"No saved reply '{name}'. Saved: {', '.join(r[0] for r in replies(c.gid)) or 'none'}")
    return Result(f"Saved reply (send this text as written):\n{body}", "📋 used saved reply")

async def _reply_names(inter, current):
    return [app_commands.Choice(name=r[0], value=r[0]) for r in replies(inter.guild_id or 0)
            if current.lower() in r[0]][:25]

@tree.command(name="reply", description="Send one of the server's saved replies (staff)")
@app_commands.describe(name="Saved reply", member="Mention someone with it (optional)")
@app_commands.autocomplete(name=_reply_names)
async def reply_cmd(inter: discord.Interaction, name: str, member: discord.Member = None):
    if not inter.guild or not is_staff(inter.user, inter.guild.id):
        return await inter.response.send_message("Only staff can use saved replies.", ephemeral=True)
    body = reply_get(inter.guild.id, name)
    if body is None:
        return await inter.response.send_message("No saved reply with that name.", ephemeral=True)
    await inter.response.send_message((member.mention + " " if member else "") + body,
                                      allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

@tree.command(name="replyadd", description="Save a reply the team can reuse (staff)")
async def replyadd_cmd(inter: discord.Interaction, name: str, text: app_commands.Range[str, 2, 1900]):
    if not inter.guild or not is_staff(inter.user, inter.guild.id):
        return await inter.response.send_message("Only staff can save replies.", ephemeral=True)
    db.execute("INSERT OR REPLACE INTO saved_replies (guild_id, name, body, by_id, uses, created) VALUES (?,?,?,?,0,?)",
               (inter.guild.id, _slug(name), text.replace("\\n", "\n"), inter.user.id, time.time()))
    db.commit()
    await inter.response.send_message(f"Saved **{_slug(name)}**.", ephemeral=True)

# ---------------------------------------------------------------------------------------------------------------
# staff notes + member lookup
# ---------------------------------------------------------------------------------------------------------------
@tool("staff_note", "staff_note | @user | <note>", "(staff) add a private staff note about a member (order issues, "
      "trust, agreements). Members never see it.", when=staff_ok, progress="📝 Saving note…")
@_mod
async def t_note(c, a):
    m = await find_member(c.guild, a[0] if a else "")
    uid = m.id if m else first_id(a[0] if a else "")
    if not uid or len(a) < 2:
        return Result("Give the member and the note.")
    db.execute("INSERT INTO staff_notes (guild_id, user_id, note, by_id, ts) VALUES (?,?,?,?,?)",
               (c.gid, uid, " | ".join(a[1:]).strip()[:500], c.user.id, time.time()))
    db.commit()
    return Result("Note saved (staff only).", "📝 saved staff note")

def notes(gid, uid, n=10):
    return db.execute("SELECT note, by_id, ts FROM staff_notes WHERE guild_id=? AND user_id=? ORDER BY ts DESC LIMIT ?",
                      (gid, uid, n)).fetchall()

@tool("member_info", "member_info | @user", "(staff) member lookup: account age, join date, roles, timeout, warnings, "
      "mod cases and staff notes", when=staff_ok, progress="🔍 Looking up member…")
@_mod
async def t_info(c, a):
    m = await find_member(c.guild, a[0] if a else "")
    if not m:
        return Result("Member not found.")
    now = datetime.now(timezone.utc)
    age = (now - m.created_at).days
    joined = (now - m.joined_at).days if m.joined_at else None
    warns = store.warn_list(c.gid, m.id)
    cases = store.case_list(c.gid, m.id)
    ns = notes(c.gid, m.id)
    out = [f"{m} (id {m.id}){' — BOT' if m.bot else ''}",
           f"account age: {age} days{' ⚠️ new account' if age < 7 else ''}; joined {joined} days ago" if joined is not None
           else f"account age: {age} days",
           "roles: " + (", ".join(r.name for r in m.roles[1:][-12:]) or "none"),
           f"timed out until <t:{int(m.timed_out_until.timestamp())}:R>" if m.is_timed_out() else "not timed out",
           f"warnings: {len(warns)}; mod cases: {len(cases)}" +
           ("; latest: " + "; ".join(f"{k}: {r}" for _, k, r, _, _ in cases[:3]) if cases else ""),
           "staff notes: " + ("; ".join(f"{n} (<t:{int(ts)}:R>)" for n, _, ts in ns[:5]) or "none")]
    return Result("\n".join(out), f"🔍 looked up {m.display_name}")

# ---------------------------------------------------------------------------------------------------------------
# hand-off to a human + summaries
# ---------------------------------------------------------------------------------------------------------------
_last_handoff = {}

def staff_roles(gid):
    g = store.guild(gid)
    return list(dict.fromkeys(list(g["tickets"]["roles"]) + list(g["mod"]["mod_roles"])))

@tool("handoff", "handoff | <why a human is needed, one line>",
      "call a human staff member into this chat: payment confirmation, refunds, account delivery problems, angry "
      "customer, anything you can't finish with tools. Pings the staff role once.", when=lambda c: bool(c.gid),
      progress="🙋 Calling staff…")
async def t_handoff(c, a):
    reason = " | ".join(a).strip() or "customer needs a staff member"
    key = getattr(c.channel, "id", 0)
    if time.time() - _last_handoff.get(key, 0) < 600:
        return Result("Staff were already called here in the last 10 minutes — tell the customer they're coming.")
    _last_handoff[key] = time.time()
    roles = [c.guild.get_role(r) for r in staff_roles(c.gid)]
    roles = [r for r in roles if r]
    ping = " ".join(r.mention for r in roles[:3])
    e = discord.Embed(title="🙋 Staff needed", description=f"{c.user.mention}: {reason}"[:1000], color=0xF59E0B)
    await safe_send(c.channel, ping or None, embed=e,
                    allowed_mentions=discord.AllowedMentions(roles=roles[:3], users=False, everyone=False))
    if not roles:
        await report(c.guild, "🙋 Staff needed", f"In {c.channel.mention}: {reason}", ping_mods=True)
    return Result("Staff have been called (pinged in this channel). Tell the customer a staff member will reply soon; "
                  "don't promise a time.", "🙋 called staff")

SUMMARY = ("Summarise this Discord support conversation for a staff member taking over. Use short bullets: who the "
           "customer is, what they want (product/plan, quantity), what was already answered (prices quoted), payment "
           "status, open problems, and the next step. Only facts from the messages; write 'unknown' when not stated.")

@tool("ticket_summary", "ticket_summary | <messages to read, optional, default 60>",
      "(staff) summarise this channel/ticket so far for a hand-over", when=staff_ok, progress="🧾 Summarising…")
async def t_summary(c, a):
    n = int(re.sub(r"\D", "", a[0] if a else "") or 60)
    lines = await channel_lines(c.channel, max(10, min(200, n)))
    if not lines:
        return Result("Nothing to summarise here.")
    text = "\n".join(lines)[-14000:]
    out = await one_shot(SUMMARY, text, max_tokens=500, temperature=0.2)
    return Result(out, "🧾 summarised")

# ---------------------------------------------------------------------------------------------------------------
# extra moderation
# ---------------------------------------------------------------------------------------------------------------
def _role(guild, text):
    rid = first_id(text)
    if rid:
        return guild.get_role(rid)
    t = (text or "").strip().lstrip("@").lower()
    return discord.utils.find(lambda r: r.name.lower() == t, guild.roles) or \
        discord.utils.find(lambda r: t and t in r.name.lower(), guild.roles)

@tool("mod_role", "mod_role | @user | add/remove | <role name or @role>", "(staff) give or take a role (e.g. Customer, "
      "Verified, Muted). Never admin/staff roles above you.", when=staff_ok, progress="🎭 Updating roles…")
@_mod
async def t_role(c, a):
    _need(c, "manage_roles")
    a = a + [""] * (3 - len(a))
    member = await _target(c, a[0])
    role = _role(c.guild, a[2])
    if not role:
        return Result(f"Role '{a[2]}' not found.")
    me = c.guild.me
    if role >= me.top_role or (role >= c.user.top_role and c.guild.owner_id != c.user.id):
        return Result(f"I can't manage **{role.name}** (it's at or above my/your top role).")
    if role.permissions.administrator or role.permissions.manage_guild:
        return Result(f"**{role.name}** has admin powers — an admin must give that by hand.")
    add = not a[1].strip().lower().startswith(("rem", "take", "del", "-"))
    if add:
        await member.add_roles(role, reason=f"{c.user}: via bot")
    else:
        await member.remove_roles(role, reason=f"{c.user}: via bot")
    store.case_add(c.gid, member.id, "role", f"{'+' if add else '-'}{role.name}", c.user.id)
    return Result(f"{'Gave' if add else 'Removed'} **{role.name}** {'to' if add else 'from'} {member.display_name}.",
                  f"🎭 {'+' if add else '-'}{role.name} {member.display_name}")

@tool("mod_nick", "mod_nick | @user | <new nickname, or 'reset'>", "(staff) change or reset a member's nickname",
      when=staff_ok, progress="✏️ Changing nickname…")
@_mod
async def t_nick(c, a):
    _need(c, "manage_nicknames")
    member = await _target(c, a[0] if a else "")
    nick = " | ".join(a[1:]).strip()
    nick = None if not nick or nick.lower() == "reset" else nick[:32]
    await member.edit(nick=nick, reason=f"{c.user}: via bot")
    return Result(f"Nickname {'reset' if nick is None else 'set to ' + nick} for {member}.", "✏️ changed nickname")

@tool("mod_unban", "mod_unban | <user id> | <reason, optional>", "(staff) unban a user by ID", when=staff_ok,
      progress="🔓 Unbanning…")
@_mod
async def t_unban(c, a):
    _need(c, "ban_members")
    uid = first_id(a[0] if a else "")
    if not uid:
        return Result("Give the user's ID.")
    reason = " | ".join(a[1:]).strip() or "unbanned"
    try:
        await c.guild.unban(discord.Object(uid), reason=f"{c.user}: {reason}")
    except discord.NotFound:
        return Result("That user isn't banned.")
    store.case_add(c.gid, uid, "unban", reason, c.user.id)
    return Result(f"Unbanned {uid}.", "🔓 unbanned")

@tool("mod_softban", "mod_softban | @user | <reason>", "(staff) ban+unban to wipe a spammer's last day of messages "
      "(they can rejoin)", when=staff_ok, progress="🧽 Softbanning…")
@_mod
async def t_softban(c, a):
    _need(c, "ban_members")
    member = await _target(c, a[0] if a else "")
    reason = " | ".join(a[1:]).strip() or "spam cleanup"
    await member.ban(reason=f"{c.user}: softban: {reason}", delete_message_seconds=86400)
    await c.guild.unban(member, reason="softban")
    store.case_add(c.gid, member.id, "softban", reason, c.user.id)
    await report(c.guild, f"🧽 {member} softbanned", f"By {c.user.mention}: {reason}", color=0xEF4444)
    return Result(f"Softbanned {member.display_name} (messages from the last day removed).", "🧽 softbanned")
