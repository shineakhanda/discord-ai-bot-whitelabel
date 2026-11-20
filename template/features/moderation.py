"""AI admin: watches chat, warns like a fair human mod, escalates, reports to staff; staff tools; welcome messages."""
import re, json, time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Optional, Literal

import discord
from discord import app_commands

import store, workspace
from store import log
from core import (client, tree, embed, is_owner, is_admin, is_staff, has_perm, find_member, safe_send, limit_text,
                  first_id, on_startup)
from agent import tool, Result, one_shot, channel_lines
from timeparse import duration

# =====================================================================
# cheap first-pass checks (no AI cost)
# =====================================================================
BAD_WORDS = [
    # English
    "fuck", "fucking", "motherfucker", "bitch", "bastard", "asshole", "cunt", "slut", "whore", "retard", "nigger",
    "nigga", "faggot", "dickhead",
    # Banglish
    "magi", "khanki", "chuda", "chudi", "choda", "chudir", "bokachoda", "boka choda", "madarchod", "madarchud",
    "bhenchod", "banchod", "harami", "haramzada", "haramjada", "shuorer", "kuttar bacha", "gandu", "tor mare",
    # Bangla script
    "মাগি", "খানকি", "চুদ", "বোকাচোদা", "হারামি", "হারামজাদা", "শুয়োর", "কুত্তার বাচ্চা", "মাদারচোদ",
]
SCAM = re.compile(r"(free\s+nitro|nitro\s+(for\s+)?free|steam\s+gift|gift\s+card\s+giveaway|claim\s+your\s+(nitro|prize)|"
                  r"airdrop|crypto\s+giveaway|d[il1][s5]c[o0]r[dcl]{1,2}[-.](?!com\b|gg\b|media\b)[a-z]+|"
                  r"steamcommun[il1]ty|steamcommunlty|discorcl|dlscord|disc0rd)", re.I)
INVITE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/[a-z0-9-]+", re.I)
LINK = re.compile(r"https?://([a-z0-9.-]+)", re.I)
EMOJI = re.compile(r"<a?:\w+:\d+>|[\U0001F300-\U0001FAFF\u2600-\u27BF]")
ZALGO = re.compile(r"[\u0300-\u036F\u0489\u1AB0-\u1AFF\u20D0-\u20FF]")

def _domain_ok(host, allowed):
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in allowed + ["discord.com", "discord.gg", "discordapp.com",
                                                                      "cdn.discordapp.com", "media.discordapp.net"])

_recent = defaultdict(lambda: deque(maxlen=12))          # (gid, uid) -> [(ts, content)]

def first_pass(msg, g):
    """Returns (hard_reasons, soft_reasons)."""
    hard, soft = [], []
    key = (msg.guild.id, msg.author.id)
    now = time.time()
    q = _recent[key]
    q.append((now, msg.content.strip().lower()))
    recent = [c for t, c in q if now - t < 8]
    if len(recent) >= 7:
        hard.append("message flooding (7+ messages in 8 seconds)")
    same = [c for t, c in q if now - t < 30 and c and c == q[-1][1]]
    if len(same) >= 4 and len(q[-1][1]) > 3:
        hard.append("repeating the same message")
    mentions = len({u.id for u in msg.mentions if not u.bot}) + len(msg.role_mentions)
    if mentions >= 6:
        hard.append(f"mass mentions ({mentions})")
    if msg.mention_everyone and not has_perm(msg.author, "mention_everyone"):
        soft.append("tried to ping everyone")
    if SCAM.search(msg.content):
        hard.append("scam / phishing link or message")
    if not g["mod"]["invites"] and INVITE.search(msg.content):
        soft.append("server invite link")
    low = msg.content.lower()
    words = BAD_WORDS + [w.lower() for w in g["mod"]["words"]]
    if any(re.search(rf"(?<![a-z]){re.escape(w)}", low) for w in words):
        soft.append("possible abusive language")
    links = LINK.findall(msg.content)
    mode = g["mod"]["links"]
    if links and mode == "block":
        hard.append("links are not allowed here")
    elif links and mode == "allowlist" and not all(_domain_ok(h, g["mod"]["allow_domains"]) for h in links):
        hard.append("that link isn't on this server's allowed list")
    if len(EMOJI.findall(msg.content)) > 25:
        hard.append("emoji spam")
    if len(ZALGO.findall(msg.content)) > 15:
        hard.append("glitch/zalgo text spam")
    if msg.content.count("\n") > 30:
        hard.append("wall of empty lines / text spam")
    letters = [ch for ch in msg.content if ch.isalpha()]
    if len(letters) > 25 and sum(ch.isupper() for ch in letters) / len(letters) > 0.8:
        soft.append("all caps")
    return hard, soft

# =====================================================================
# AI judgement
# =====================================================================
JUDGE = """You are __BOT_NAME__, a fair, calm human-like moderator of a Discord server.
Decide if the LAST message breaks the rules. Friendly banter, jokes between friends and casual swearing that is not aimed
at someone are usually fine. Harassment, slurs, threats, hate, NSFW, scams, spam and advertising are not.
Strictness: {strict}. Server rules / info from admins: {rules}
Return ONLY JSON:
{{"violation": true/false, "severity": 1-3, "rule": "short rule name", "delete": true/false,
  "warning": "1-2 sentence warning to the user, in the same language/script they wrote, firm but kind, no insults"}}
severity: 1 = minor (warn), 2 = clear violation (warn + delete), 3 = serious (slurs, threats, scams, NSFW)."""

async def judge(msg, reasons, g):
    try:
        ctx = await channel_lines(msg.channel, 5, before=msg)
    except discord.HTTPException:
        ctx = []
    convo = "\n".join(ctx[-4:]) + f"\nLAST MESSAGE — {msg.author.display_name}: {msg.content[:600]}"
    raw = await one_shot(JUDGE.format(strict=g["mod"]["strictness"], rules=(g["knowledge"] or "standard Discord rules")[:800]),
                         f"Signals: {', '.join(reasons)}\n{convo}", 200)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        v = json.loads(m.group())
        return v if v.get("violation") else None
    except Exception:
        return None

# =====================================================================
# actions
# =====================================================================
TEMPLATES = {
    "en": "{m} hey, please stop — {r}. Keep it chill or I'll have to step in. 🙏",
    "banglish": "{m} bhai/apu, eta korben na — {r}. Rules follow korun, naile action nite hobe. 🙏",
    "bn": "{m} দয়া করে এটা করবেন না — {r}। নিয়ম মেনে চলুন, না হলে ব্যবস্থা নিতে হবে। 🙏",
}

def can_act_on(target, actor=None):
    me = target.guild.me
    if target.id == target.guild.owner_id or is_owner(target):
        return False
    if target.top_role >= me.top_role:
        return False
    if actor is not None and isinstance(actor, discord.Member) and actor.id != target.guild.owner_id \
            and not is_owner(actor) and target.top_role >= actor.top_role:
        return False
    return True

async def report(guild, title, desc, ping_mods=False, color=0xF59E0B):
    g = store.guild(guild.id)
    ch_id = g["mod"]["log_channel"]
    workspace.note(f"guild {guild.id}: [mod] {title} — {desc[:160]}")
    if not ch_id:
        return
    ch = guild.get_channel(ch_id)
    if not ch:
        return
    roles = [guild.get_role(r) for r in g["mod"]["mod_roles"]]
    roles = [r for r in roles if r]
    content = " ".join(r.mention for r in roles) if ping_mods and roles else None
    await safe_send(ch, content, embed=embed(title, desc, color=color),
                    allowed_mentions=discord.AllowedMentions(roles=roles if ping_mods else False))

async def apply_warning(guild, member, channel, reason, by, warning_text=None, severity=1, message=None):
    """Record a warning, tell the member, escalate with timeouts, report to staff. Returns a summary."""
    g = store.guild(guild.id)
    store.warn_add(guild.id, member.id, reason, getattr(by, "id", 0))
    store.case_add(guild.id, member.id, "warn", reason, getattr(by, "id", 0))
    count = len(store.warn_list(guild.id, member.id, since=time.time() - 86400))
    done = [f"warned ({count} in 24h)"]
    if message and g["mod"]["delete"] and severity >= 2:
        try:
            await message.delete(); done.append("message deleted")
        except discord.HTTPException:
            pass
    text = warning_text or TEMPLATES.get(g["lang"], TEMPLATES["en"]).format(m="{m}", r=reason)
    text = text.replace("{m}", member.mention) if "{m}" in text else f"{member.mention} {text}"
    if channel:
        await safe_send(channel, text, allowed_mentions=discord.AllowedMentions(users=[member]))
    if g["mod"]["dm_warn"]:
        try:
            await member.send(f"⚠️ Warning in **{guild.name}**: {reason}\n-# Think it was a mistake? Use `/appeal` "
                              f"here and the mods will review it.")
        except discord.HTTPException:
            pass
    mins = 60 if severity >= 3 else 60 if count >= 5 else 10 if count >= 3 else 0
    if mins and g["mod"]["timeout"]:
        try:
            if can_act_on(member):
                await member.timeout(timedelta(minutes=mins), reason=f"__BOT_NAME__: {reason}")
                done.append(f"timed out {mins}m")
                store.case_add(guild.id, member.id, "timeout", f"{mins}m auto: {reason}", client.user.id)
            else:
                done.append("can't timeout (role too high)")
        except Exception:
            done.append("timeout failed (check my permissions/role)")
    jump = f"\n[Jump to chat]({message.jump_url})" if message else ""
    excerpt = f"\n> {message.content[:300]}" if message and message.content else ""
    await report(guild, f"⚠️ {member} — {reason}",
                 f"User: {member.mention} (`{member.id}`)\nChannel: {getattr(channel, 'mention', '?')}\n"
                 f"By: {getattr(by, 'mention', '__BOT_NAME__ auto-mod')}\nActions: {', '.join(done)}{excerpt}{jump}",
                 ping_mods=severity >= 3 or len(done) > 2, color=0xEF4444 if severity >= 3 else 0xF59E0B)
    return ", ".join(done)

def exempt(msg, g):
    m = msg.author
    if not isinstance(m, discord.Member) or m.bot or is_owner(m) or is_admin(m) or is_staff(m, msg.guild.id):
        return True
    if msg.channel.id in g["mod"]["exempt_channels"] or getattr(msg.channel, "parent_id", None) in g["mod"]["exempt_channels"]:
        return True
    return any(r.id in g["mod"]["exempt_roles"] for r in m.roles)

async def check_message(msg):
    """Called for every guild message. Returns True if __BOT_NAME__ took action (stop processing)."""
    g = store.guild(msg.guild.id)
    if not g["mod"]["enabled"] or not g["features"].get("moderation", True) or exempt(msg, g):
        return False
    hard, soft = first_pass(msg, g)
    if hard:
        sev = 3 if any("scam" in r or "mass" in r for r in hard) else 2
        await apply_warning(msg.guild, msg.author, msg.channel, hard[0], client.user, severity=sev, message=msg)
        _recent[(msg.guild.id, msg.author.id)].clear()
        return True
    if not soft or g["mod"]["strictness"] == "low" and soft == ["all caps"]:
        return False
    if not store.quota(msg.guild.id, msg.author.id, "mod"):
        return False
    v = await judge(msg, soft, g)
    if not v:
        return False
    sev = int(v.get("severity", 1) or 1)
    if g["mod"]["strictness"] == "low" and sev < 2:
        return False
    await apply_warning(msg.guild, msg.author, msg.channel, v.get("rule") or soft[0], client.user,
                        warning_text=v.get("warning"), severity=max(sev, 2 if v.get("delete") else sev), message=msg)
    return True

# =====================================================================
# staff tools for the agent
# =====================================================================
def staff_ok(c):
    return bool(c.gid and c.guild and is_staff(c.user, c.gid))

async def _target(c, text):
    m = await find_member(c.guild, text)
    if not m:
        raise ValueError("Member not found. Mention them or give their ID.")
    if not can_act_on(m, c.user):
        raise ValueError(f"I can't act on {m.display_name} (same or higher role than you or me, or the owner).")
    return m

def _need(c, perm):
    if not has_perm(c.user, perm):
        raise ValueError(f"You need the **{perm.replace('_', ' ')}** permission for that.")

def _mod(fn):
    async def inner(c, a):
        try:
            return await fn(c, a)
        except ValueError as e:
            return Result(str(e))
        except discord.Forbidden:
            return Result("Discord says I'm missing permissions for that (check my role is high enough).")
    return inner

WARN_WRITER = ("You are __BOT_NAME__, a fair human-like Discord moderator. Write a short warning (1-2 sentences) to the member "
               "for the given reason, firm but kind, no insults, in this language: {lang}. Output only the warning.")

@tool("mod_warn", "mod_warn | @user | <reason>", "warn a member (posts a calm warning, logs it, escalates)",
      when=staff_ok, progress="⚠️ Warning member…")
@_mod
async def t_warn(c, a):
    member = await _target(c, a[0])
    reason = " | ".join(a[1:]).strip() or "breaking the server rules"
    lang = {"bn": "Bangla script", "banglish": "Banglish", "en": "English"}.get(store.guild(c.gid)["lang"], "English")
    text = await one_shot(WARN_WRITER.format(lang=lang), f"Member: {member.display_name}\nReason: {reason}", 120)
    done = await apply_warning(c.guild, member, c.channel, reason, c.user, warning_text=text)
    return Result(f"Done: {member.display_name} {done}.", f"⚠️ warned {member.display_name}")

@tool("mod_warnings", "mod_warnings | @user", "a member's warning history", when=staff_ok, progress="📋 Checking warnings…")
@_mod
async def t_warnings(c, a):
    member = await find_member(c.guild, a[0])
    if not member:
        return Result("Member not found.")
    rows = store.warn_list(c.gid, member.id)
    return Result(f"{member.display_name}: {len(rows)} warning(s). " +
                  "; ".join(f"<t:{int(ts)}:R> {r}" for _, r, _, ts in rows[:10]), "📋 checked warnings")

@tool("mod_cases", "mod_cases | @user", "full moderation history (warns, timeouts, kicks, bans)", when=staff_ok,
      progress="📋 Checking mod history…")
@_mod
async def t_cases(c, a):
    member = await find_member(c.guild, a[0])
    uid = member.id if member else first_id(a[0])
    if not uid:
        return Result("Member not found.")
    rows = store.case_list(c.gid, uid)
    return Result(f"{len(rows)} case(s): " + "; ".join(f"#{i} {k} <t:{int(ts)}:R>: {r}" for i, k, r, _, ts in rows[:15]),
                  "📋 checked history")

@tool("mod_timeout", "mod_timeout | @user | <10m / 1h / 1d> | <reason>", "timeout (mute) a member", when=staff_ok,
      progress="🔇 Timing out member…")
@_mod
async def t_timeout(c, a):
    _need(c, "moderate_members")
    member = await _target(c, a[0])
    secs = duration(a[1] if len(a) > 1 else "10m") or 600
    secs = min(secs, 28 * 86400)
    reason = " | ".join(a[2:]).strip() or "moderation"
    await member.timeout(timedelta(seconds=secs), reason=f"{c.user}: {reason}")
    store.case_add(c.gid, member.id, "timeout", f"{secs // 60}m: {reason}", c.user.id)
    await report(c.guild, f"🔇 {member} timed out", f"{member.mention} for {secs // 60} min by {c.user.mention}: {reason}")
    return Result(f"{member.display_name} timed out for {secs // 60} min.", f"🔇 timed out {member.display_name}")

@tool("mod_untimeout", "mod_untimeout | @user", "remove a timeout", when=staff_ok, progress="🔊 Removing timeout…")
@_mod
async def t_untimeout(c, a):
    _need(c, "moderate_members")
    member = await _target(c, a[0])
    await member.timeout(None, reason=f"{c.user}: untimeout")
    return Result(f"{member.display_name} can talk again.", f"🔊 untimed {member.display_name}")

@tool("mod_kick", "mod_kick | @user | <reason>", "kick a member (only when a mod clearly asks)", when=staff_ok,
      progress="👢 Kicking member…")
@_mod
async def t_kick(c, a):
    _need(c, "kick_members")
    member = await _target(c, a[0])
    reason = " | ".join(a[1:]).strip() or "moderation"
    await member.kick(reason=f"{c.user}: {reason}")
    store.case_add(c.gid, member.id, "kick", reason, c.user.id)
    await report(c.guild, f"👢 {member} kicked", f"By {c.user.mention}: {reason}", color=0xEF4444)
    return Result(f"Kicked {member.display_name}.", f"👢 kicked {member.display_name}")

@tool("mod_ban", "mod_ban | @user | <reason>", "ban a member (only when a mod clearly asks)", when=staff_ok,
      progress="🔨 Banning member…")
@_mod
async def t_ban(c, a):
    _need(c, "ban_members")
    member = await _target(c, a[0])
    reason = " | ".join(a[1:]).strip() or "moderation"
    await member.ban(reason=f"{c.user}: {reason}", delete_message_seconds=3600)
    store.case_add(c.gid, member.id, "ban", reason, c.user.id)
    await report(c.guild, f"🔨 {member} banned", f"By {c.user.mention}: {reason}", color=0xEF4444)
    return Result(f"Banned {member.display_name}.", f"🔨 banned {member.display_name}")

@tool("mod_purge", "mod_purge | <count 1-100> | <@user, optional>", "delete recent messages here", when=staff_ok,
      progress="🧹 Cleaning messages…")
@_mod
async def t_purge(c, a):
    _need(c, "manage_messages")
    n = max(1, min(100, int(re.sub(r"\D", "", a[0]) or 10)))
    uid = first_id(a[1]) if len(a) > 1 else None
    check = (lambda m: m.author.id == uid) if uid else (lambda m: True)
    deleted = await c.channel.purge(limit=min(n * 5, 500) if uid else n, check=check,
                                    before=c.message if c.message else None)
    deleted = deleted[:n]
    await report(c.guild, "🧹 Purge", f"{len(deleted)} messages in {c.channel.mention} by {c.user.mention}")
    return Result(f"Deleted {len(deleted)} messages.", f"🧹 purged {len(deleted)}")

@tool("mod_slowmode", "mod_slowmode | <seconds, 0 = off>", "set slowmode in this channel", when=staff_ok,
      progress="🐢 Setting slowmode…")
@_mod
async def t_slow(c, a):
    _need(c, "manage_channels")
    secs = max(0, min(21600, int(re.sub(r"\D", "", a[0]) or 0)))
    await c.channel.edit(slowmode_delay=secs)
    return Result(f"Slowmode {'off' if not secs else f'{secs}s'} in #{c.channel.name}.", "🐢 slowmode")

@tool("mod_lock", "mod_lock | on/off", "lock or unlock this channel for @everyone", when=staff_ok,
      progress="🔒 Updating channel lock…")
@_mod
async def t_lock(c, a):
    _need(c, "manage_channels")
    on = (a[0].lower() if a else "on") in ("on", "lock", "yes", "true")
    ow = c.channel.overwrites_for(c.guild.default_role)
    ow.send_messages = False if on else None
    await c.channel.set_permissions(c.guild.default_role, overwrite=ow, reason=f"{c.user}: {'lock' if on else 'unlock'}")
    await report(c.guild, "🔒 Channel locked" if on else "🔓 Channel unlocked", f"{c.channel.mention} by {c.user.mention}")
    return Result(f"#{c.channel.name} {'locked' if on else 'unlocked'}.", "🔒 locked" if on else "🔓 unlocked")

# =====================================================================
# slash: warnings, report
# =====================================================================
async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

@tree.command(name="warn", description="Warn a member (__BOT_NAME__ writes a calm warning and logs it)")
@app_commands.guild_only()
async def warn_cmd(inter: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 3, 300]):
    if not is_staff(inter.user, inter.guild_id):
        return await _deny(inter, "Only staff can warn.")
    if not can_act_on(member, inter.user):
        return await _deny(inter, "You can't warn someone with the same or a higher role.")
    await inter.response.defer(ephemeral=True, thinking=True)
    lang = {"bn": "Bangla script", "banglish": "Banglish", "en": "English"}.get(store.guild(inter.guild_id)["lang"], "English")
    try:
        text = await one_shot(WARN_WRITER.format(lang=lang), f"Member: {member.display_name}\nReason: {reason}", 120)
    except Exception:
        text = None
    done = await apply_warning(inter.guild, member, inter.channel, reason, inter.user, warning_text=text)
    await inter.followup.send(f"✅ {member.display_name}: {done}", ephemeral=True)

@tree.command(name="warnings", description="See a member's warnings")
@app_commands.guild_only()
async def warnings_cmd(inter: discord.Interaction, member: discord.Member):
    if not is_staff(inter.user, inter.guild_id):
        return await _deny(inter, "Only staff can see warnings.")
    rows = store.warn_list(inter.guild_id, member.id)
    body = "\n".join(f"`#{i}` <t:{int(ts)}:R> — {r} (by <@{b}>)" for i, r, b, ts in rows[:20]) or "No warnings. 😇"
    await inter.response.send_message(embed=embed(f"⚠️ Warnings for {member.display_name} ({len(rows)})", body),
                                      ephemeral=True)

@tree.command(name="clearwarnings", description="Clear a member's warnings")
@app_commands.guild_only()
async def clearwarnings_cmd(inter: discord.Interaction, member: discord.Member):
    if not is_staff(inter.user, inter.guild_id):
        return await _deny(inter, "Only staff can clear warnings.")
    n = store.warn_clear(inter.guild_id, member.id)
    await report(inter.guild, "🧽 Warnings cleared", f"{member.mention}: {n} cleared by {inter.user.mention}", color=0x22C55E)
    await inter.response.send_message(f"🧽 Cleared {n} warning(s) for {member.display_name}.", ephemeral=True)

@tree.context_menu(name="Report to mods (__BOT_NAME__)")
@app_commands.guild_only()
async def report_ctx(inter: discord.Interaction, message: discord.Message):
    g = store.guild(inter.guild_id)
    await inter.response.defer(ephemeral=True, thinking=True)
    verdict = None
    if message.content and store.quota(inter.guild_id, inter.user.id, "mod"):
        try:
            verdict = await judge(message, ["reported by a member"], g)
        except Exception:
            verdict = None
    sev = int(verdict.get("severity", 1)) if verdict else 1
    await report(inter.guild, f"🚩 Report: message by {message.author}",
                 f"Reporter: {inter.user.mention}\nAuthor: {message.author.mention}\nChannel: {message.channel.mention}\n"
                 f"__BOT_NAME__'s view: {verdict.get('rule') + f' (severity {sev})' if verdict else 'no clear violation'}\n"
                 f"> {message.content[:400]}\n[Jump]({message.jump_url})", ping_mods=sev >= 2)
    if not g["mod"]["log_channel"]:
        return await inter.followup.send("Thanks! This server hasn't set a mod log channel yet, so please tell a mod "
                                         "directly.", ephemeral=True)
    await inter.followup.send("✅ Thanks, the mods have been notified.", ephemeral=True)

# =====================================================================
# welcome (needs MEMBERS_INTENT=1 and Server Members Intent in the Developer Portal)
# =====================================================================
WELCOME = ("You are __BOT_NAME__, the friendly AI member of this Discord server. Write a short, warm, human welcome "
           "(1-2 sentences, max 1 emoji) for a new member. Language: {lang}. Mention nothing you don't know. "
           "Admin notes: {note}. Output only the message.")

_joins = defaultdict(lambda: deque(maxlen=50))

async def raid_mode(guild, on, by=None):
    """Raise verification to High during a raid (on) or restore the previous level (off)."""
    m = store.guild(guild.id)["mod"]
    try:
        if on:
            if m["raid_prev_level"] is None:
                m["raid_prev_level"] = int(guild.verification_level.value)
            await guild.edit(verification_level=discord.VerificationLevel.high, reason="__BOT_NAME__ raid mode")
        else:
            prev = m["raid_prev_level"]
            await guild.edit(verification_level=discord.VerificationLevel(prev if prev is not None else 1),
                             reason="__BOT_NAME__ raid mode off")
            m["raid_prev_level"] = None
        store.save_guild(guild.id)
        store.case_add(guild.id, 0, "raidmode", "on" if on else "off", getattr(by, "id", 0))
        return True
    except discord.HTTPException:
        return False

async def join_checks(member):
    """Raid + new-account checks. Returns True if the member was kicked."""
    g = store.guild(member.guild.id)
    m = g["mod"]
    if not m["enabled"]:
        return False
    now = time.time()
    q = _joins[member.guild.id]
    q.append(now)
    recent = [t for t in q if now - t < 60]
    if len(recent) >= m["raid_joins"] and now - _raid_alert.get(member.guild.id, 0) > 600:
        _raid_alert[member.guild.id] = now
        locked = m["raid_action"] == "lock" and await raid_mode(member.guild, True)
        await report(member.guild, "🚨 Possible raid", f"{len(recent)} members joined in 60 seconds."
                     + ("\nI raised verification to **High**. Use `/raidmode off` when it's over." if locked else
                        "\nUse `/raidmode on` to raise verification, `/mod_lock`-style lockdown via asking me."),
                     ping_mods=True, color=0xEF4444)
    if m["min_age_days"] and not member.bot:
        age = (discord.utils.utcnow() - member.created_at).days
        if age < m["min_age_days"]:
            act = "flagged"
            if m["new_account_action"] == "kick" and can_act_on(member):
                try:
                    try:
                        await member.send(f"Hi! **{member.guild.name}** only accepts accounts older than "
                                          f"{m['min_age_days']} days. Please join again later. 🙏")
                    except discord.HTTPException:
                        pass
                    await member.kick(reason=f"__BOT_NAME__: account younger than {m['min_age_days']} days")
                    store.case_add(member.guild.id, member.id, "kick", "new account", client.user.id)
                    act = "kicked"
                except discord.HTTPException:
                    act = "kick failed"
            await report(member.guild, f"🆕 New account {act}", f"{member.mention} (`{member.id}`) — account is "
                         f"{age} day(s) old (minimum {m['min_age_days']}).")
            return act == "kicked"
    return False

_raid_alert = {}

@client.event
async def on_member_join(member):
    try:
        if await join_checks(member):
            return
    except Exception as e:
        log.warning("join checks failed: %s", e)
    g = store.guild(member.guild.id)
    w = g["welcome"]
    if not w["enabled"] or not w["channel"] or member.bot:
        return
    ch = member.guild.get_channel(w["channel"])
    if not ch or not store.quota(member.guild.id, member.id, "chat"):
        return
    lang = {"bn": "Bangla script", "banglish": "Banglish", "en": "English"}.get(g["lang"], "English")
    try:
        text = await one_shot(WELCOME.format(lang=lang, note=w["note"][:300] or "none"),
                              f"Server: {member.guild.name} ({member.guild.member_count} members)\nNew member: "
                              f"{member.display_name}", 120)
    except Exception:
        text = f"Welcome to **{member.guild.name}**, {{m}}! 🎉"
    text = text.replace("{m}", member.mention)
    if member.mention not in text:
        text = f"{member.mention} {text}"
    await safe_send(ch, text, allowed_mentions=discord.AllowedMentions(users=[member]))


# =====================================================================
# mod slash commands (fast, no AI)
# =====================================================================
async def _staff(inter, perm):
    if not is_staff(inter.user, inter.guild_id) or not has_perm(inter.user, perm):
        await _deny(inter, f"You need **{perm.replace('_', ' ')}** for that."); return False
    return True

@tree.command(name="modlog", description="A member's full moderation history")
@app_commands.guild_only()
async def modlog_cmd(inter: discord.Interaction, member: discord.User):
    if not is_staff(inter.user, inter.guild_id):
        return await _deny(inter, "Only staff can see this.")
    rows = store.case_list(inter.guild_id, member.id)
    icon = {"warn": "⚠️", "timeout": "🔇", "kick": "👢", "ban": "🔨", "unban": "♻️", "appeal": "📨"}
    body = "\n".join(f"`#{i}` {icon.get(k, '•')} **{k}** <t:{int(ts)}:R> — {r} (by <@{b}>)"
                      for i, k, r, b, ts in rows) or "Clean record. 😇"
    await inter.response.send_message(embed=embed(f"📋 Mod log: {member}", body[:4000]), ephemeral=True)

@tree.command(name="timeout", description="Timeout a member")
@app_commands.guild_only()
@app_commands.describe(duration="e.g. 10m, 1h, 1d (max 28d)")
async def timeout_cmd(inter: discord.Interaction, member: discord.Member, duration: str = "10m",
                      reason: str = "moderation"):
    if not await _staff(inter, "moderate_members"):
        return
    if not can_act_on(member, inter.user):
        return await _deny(inter, "I can't act on that member (role too high).")
    secs = min(duration_s(duration) or 600, 28 * 86400)
    await member.timeout(timedelta(seconds=secs), reason=f"{inter.user}: {reason}")
    store.case_add(inter.guild_id, member.id, "timeout", f"{secs // 60}m: {reason}", inter.user.id)
    await report(inter.guild, f"🔇 {member} timed out", f"{member.mention} for {secs // 60} min by {inter.user.mention}: {reason}")
    await inter.response.send_message(f"🔇 {member.mention} timed out for {secs // 60} min.",
                                      allowed_mentions=discord.AllowedMentions.none())

def duration_s(text):
    return duration(text)

@tree.command(name="untimeout", description="Remove a member's timeout")
@app_commands.guild_only()
async def untimeout_cmd(inter: discord.Interaction, member: discord.Member):
    if not await _staff(inter, "moderate_members"):
        return
    await member.timeout(None, reason=f"{inter.user}: untimeout")
    await inter.response.send_message(f"🔊 {member.display_name} can talk again.", ephemeral=True)

@tree.command(name="kick", description="Kick a member")
@app_commands.guild_only()
async def kick_cmd(inter: discord.Interaction, member: discord.Member, reason: str = "moderation"):
    if not await _staff(inter, "kick_members"):
        return
    if not can_act_on(member, inter.user):
        return await _deny(inter, "I can't act on that member (role too high).")
    await member.kick(reason=f"{inter.user}: {reason}")
    store.case_add(inter.guild_id, member.id, "kick", reason, inter.user.id)
    await report(inter.guild, f"👢 {member} kicked", f"By {inter.user.mention}: {reason}", color=0xEF4444)
    await inter.response.send_message(f"👢 Kicked **{member}**.", ephemeral=True)

@tree.command(name="ban", description="Ban a member")
@app_commands.guild_only()
async def ban_cmd(inter: discord.Interaction, member: discord.Member, reason: str = "moderation",
                  delete_hours: app_commands.Range[int, 0, 168] = 1):
    if not await _staff(inter, "ban_members"):
        return
    if not can_act_on(member, inter.user):
        return await _deny(inter, "I can't act on that member (role too high).")
    await member.ban(reason=f"{inter.user}: {reason}", delete_message_seconds=delete_hours * 3600)
    store.case_add(inter.guild_id, member.id, "ban", reason, inter.user.id)
    await report(inter.guild, f"🔨 {member} banned", f"By {inter.user.mention}: {reason}", color=0xEF4444)
    await inter.response.send_message(f"🔨 Banned **{member}**.", ephemeral=True)

@tree.command(name="unban", description="Unban a user by ID")
@app_commands.guild_only()
async def unban_cmd(inter: discord.Interaction, user_id: str, reason: str = "appeal accepted"):
    if not await _staff(inter, "ban_members"):
        return
    uid = first_id(user_id)
    if not uid:
        return await _deny(inter, "Give the user's ID.")
    try:
        await inter.guild.unban(discord.Object(uid), reason=f"{inter.user}: {reason}")
    except discord.HTTPException:
        return await _deny(inter, "That user isn't banned (or I can't unban).")
    store.case_add(inter.guild_id, uid, "unban", reason, inter.user.id)
    await report(inter.guild, "♻️ Unban", f"<@{uid}> by {inter.user.mention}: {reason}", color=0x22C55E)
    await inter.response.send_message(f"♻️ Unbanned <@{uid}>.", ephemeral=True)

@tree.command(name="purge", description="Delete recent messages (optionally only from one member)")
@app_commands.guild_only()
async def purge_cmd(inter: discord.Interaction, count: app_commands.Range[int, 1, 200],
                    member: Optional[discord.Member] = None):
    if not await _staff(inter, "manage_messages"):
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    check = (lambda m: m.author.id == member.id) if member else (lambda m: True)
    deleted = (await inter.channel.purge(limit=count * 5 if member else count, check=check))[:count]
    await report(inter.guild, "🧹 Purge", f"{len(deleted)} messages in {inter.channel.mention} by {inter.user.mention}")
    await inter.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)

@tree.command(name="slowmode", description="Set slowmode in this channel (0 = off)")
@app_commands.guild_only()
async def slowmode_cmd(inter: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    if not await _staff(inter, "manage_channels"):
        return
    await inter.channel.edit(slowmode_delay=seconds)
    await inter.response.send_message(f"🐢 Slowmode {'off' if not seconds else f'{seconds}s'}.")

@tree.command(name="lock", description="Lock or unlock this channel for @everyone")
@app_commands.guild_only()
async def lock_cmd(inter: discord.Interaction, state: Literal["on", "off"], reason: str = ""):
    if not await _staff(inter, "manage_channels"):
        return
    ow = inter.channel.overwrites_for(inter.guild.default_role)
    ow.send_messages = False if state == "on" else None
    await inter.channel.set_permissions(inter.guild.default_role, overwrite=ow, reason=f"{inter.user}: lock {state}")
    await report(inter.guild, "🔒 Locked" if state == "on" else "🔓 Unlocked", f"{inter.channel.mention} by {inter.user.mention}")
    await inter.response.send_message(("🔒 Channel locked." if state == "on" else "🔓 Channel unlocked.")
                                      + (f" Reason: {reason}" if reason else ""))

@tree.command(name="raidmode", description="Raise verification to High during a raid, or restore it")
@app_commands.guild_only()
async def raidmode_cmd(inter: discord.Interaction, state: Literal["on", "off"]):
    if not await _staff(inter, "manage_guild"):
        return
    ok = await raid_mode(inter.guild, state == "on", inter.user)
    await report(inter.guild, f"🚨 Raid mode {state}", f"by {inter.user.mention}", ping_mods=state == "on")
    await inter.response.send_message(f"🚨 Raid mode **{state}**." if ok else "I need Manage Server permission for that.",
                                      ephemeral=not ok)

# =====================================================================
# appeals
# =====================================================================
@tree.command(name="appeal", description="Appeal a warning, timeout or ban to the server's mods")
@app_commands.describe(message="Explain what happened", server_id="Server ID (optional, defaults to your last case)")
async def appeal_cmd(inter: discord.Interaction, message: app_commands.Range[str, 10, 1000],
                     server_id: Optional[str] = None):
    gid = first_id(server_id or "") or inter.guild_id or store.case_last_guild(inter.user.id)
    guild = client.get_guild(gid) if gid else None
    if not guild:
        return await _deny(inter, "I couldn't find which server. Add the `server_id`.")
    if not store.take(f"appeal:{inter.user.id}:{gid}", "appeal", 2):
        return await _deny(inter, "You already sent appeals today. The mods will get back to you.")
    rows = store.case_list(gid, inter.user.id, 5)
    hist = "\n".join(f"#{i} {k}: {r}" for i, k, r, _, _ in rows) or "no cases"
    store.case_add(gid, inter.user.id, "appeal", message[:200], inter.user.id)
    await report(guild, f"📨 Appeal from {inter.user}", f"User: <@{inter.user.id}> (`{inter.user.id}`)\n"
                 f"Recent cases:\n{hist}\n\n**Appeal:** {message}", ping_mods=True, color=0x3B82F6)
    await inter.response.send_message(f"📨 Sent to the mods of **{guild.name}**. Please be patient. 🙏", ephemeral=True)

# =====================================================================
# daily mod digest (like a mod writing an end-of-day summary)
# =====================================================================
DIGEST = ("You are __BOT_NAME__, the server's AI moderator, writing a short end-of-day report for the staff. "
          "Summarise the moderation cases below in 3-6 bullets: what happened, repeat offenders, patterns, one suggestion. "
          "Friendly and clear, English.")

@on_startup
async def digest_loop():
    await client.wait_until_ready()
    done_day = ""
    while not client.is_closed():
        try:
            now = time.gmtime()
            day = time.strftime("%Y-%m-%d", now)
            if now.tm_hour == 17 and done_day != day:              # 23:00 Bangladesh
                done_day = day
                for guild in list(client.guilds):
                    m = store.guild(guild.id)["mod"]
                    if not (m["enabled"] and m["digest"] and m["log_channel"]):
                        continue
                    rows = store.case_recent(guild.id, time.time() - 86400)
                    if not rows:
                        continue
                    lines = [f"{k} user {u}: {r}" for _, u, k, r, _, _ in rows][:80]
                    try:
                        text = await one_shot(DIGEST, "\n".join(lines), 350)
                    except Exception:
                        text = f"{len(rows)} cases today."
                    await report(guild, f"🗒️ Daily mod report — {len(rows)} case(s)", text[:3900], color=0x6366F1)
        except Exception as e:
            log.error("digest loop: %s", e)
        await __import__("asyncio").sleep(600)
