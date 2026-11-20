"""Shared Discord client, command tree and helpers used by every feature module."""
import re, time, asyncio

import aiohttp
import discord
from discord import app_commands

import store
from store import cfg, OWNER_ID, log

COLOR = __COLOR__
COOLDOWN = 3
MAX_INPUT = 1500
EMOJI_RE = re.compile(r"<a?(:\w+:)\d+>")

# ---------- client ----------
intents = discord.Intents.default()
intents.message_content = True                     # auto-reply channels, tickets, moderation, owner commands
intents.members = store.env("MEMBERS_INTENT") == "1"   # welcome messages (privileged: enable in Developer Portal)

_startup_tasks = []

def on_startup(fn):
    """Register a coroutine to run as a background task after login setup."""
    _startup_tasks.append(fn)
    return fn

class __BOT_NAME__(discord.Client):
    def __init__(self):
        super().__init__(intents=intents, max_messages=None, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        store.HTTP["s"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
        try:
            synced = await self.tree.sync()
            log.info("synced %d slash commands", len(synced))
        except discord.HTTPException as e:
            log.warning("slash sync failed: %s", e)
        for fn in _startup_tasks:
            asyncio.create_task(fn())

    async def close(self):
        await super().close()
        if store.HTTP["s"]:
            await store.HTTP["s"].close()
        store.close_db()                 # flush the database cleanly on stop/restart (no data loss)

client = __BOT_NAME__()
tree = client.tree

# ---------- permissions ----------
def is_owner(user):
    return getattr(user, "id", None) == OWNER_ID

def perms(member):
    return member.guild_permissions if isinstance(member, discord.Member) else None

def has_perm(member, name):
    if is_owner(member):
        return True
    p = perms(member)
    return bool(p and (p.administrator or getattr(p, name, False)))

def is_admin(member):
    return has_perm(member, "manage_guild")

def has_role(member, role_id):
    return bool(role_id and isinstance(member, discord.Member) and any(r.id == role_id for r in member.roles))

def is_staff(member, gid):
    """Ticket staff or moderators."""
    if not isinstance(member, discord.Member):
        return False
    g = store.guild(gid)
    roles = set(g["tickets"]["roles"]) | set(g["mod"]["mod_roles"])
    return is_admin(member) or has_perm(member, "moderate_members") or any(r.id in roles for r in member.roles)

def can_panel(member, gid):
    p = store.guild(gid)["panel"]
    return can_panel_admin(member, gid) or has_role(member, p["role"])

def can_panel_admin(member, gid):
    p = store.guild(gid)["panel"]
    return is_admin(member) or has_role(member, p["admin_role"])

def can_panel_owner(member, gid):
    """Hosting-owner level: Discord server owner, Administrator, or the panel owner role."""
    if is_owner(member):
        return True
    if not isinstance(member, discord.Member):
        return False
    p = store.guild(gid)["panel"]
    return member.id == member.guild.owner_id or has_perm(member, "administrator") or has_role(member, p["owner_role"])

def can_announce(member, gid):
    return is_admin(member) or has_role(member, store.guild(gid)["announce"]["role"])

def blocked(user, gid):
    return user.id in cfg["blocked"] or (gid is not None and gid in cfg["blocked_guilds"])

def feature(gid, name):
    if gid is None:
        return name in ("chat", "search", "browse")
    return store.guild(gid)["features"].get(name, True)

# ---------- small helpers ----------
_last = {}

def cooled(user, table=_last, secs=COOLDOWN):
    if is_owner(user):
        return True
    now = time.time()
    if now - table.get(user.id, 0) < secs:
        return False
    table[user.id] = now
    return True

def chunks(text, n=1990):
    out, text = [], text or ""
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut]); text = text[cut:].lstrip("\n")
    out.append(text)
    return [c for c in out if c.strip()] or ["…"]


def limit_text(kind):
    return f"📉 This server reached today's **{kind}** limit. It resets daily at 00:00 UTC (6 AM Bangladesh)."

def prepare_text(msg):
    text = re.sub(rf"<@!?{client.user.id}>", "", msg.content)
    for u in msg.mentions:
        text = re.sub(rf"<@!?{u.id}>", f"@{u.display_name} (<@{u.id}>)", text)
    for r in getattr(msg, "role_mentions", []):
        text = text.replace(f"<@&{r.id}>", f"@{r.name}")
    for c in getattr(msg, "channel_mentions", []):
        text = text.replace(f"<#{c.id}>", f"#{c.name} (<#{c.id}>)")
    text = EMOJI_RE.sub(r"\1", text)
    return text.strip()[:MAX_INPUT]

ID_RE = re.compile(r"\d{15,21}")

def first_id(text):
    m = ID_RE.search(text or "")
    return int(m.group()) if m else None

async def find_member(guild, text):
    """Member from a mention, ID or name."""
    uid = first_id(text)
    if uid:
        m = guild.get_member(uid)
        if m:
            return m
        try:
            return await guild.fetch_member(uid)
        except discord.HTTPException:
            return None
    name = (text or "").strip().lstrip("@").lower()
    if not name:
        return None
    for m in getattr(guild, "members", []):
        if name in (m.name.lower(), m.display_name.lower()):
            return m
    try:
        res = await guild.query_members(query=name, limit=1)
        return res[0] if res else None
    except Exception:
        return None

def find_channel(guild, text, fallback=None):
    cid = first_id(text)
    if cid:
        ch = guild.get_channel(cid) or guild.get_thread(cid)
        if ch:
            return ch
    name = (text or "").strip().lstrip("#").lower().replace(" ", "-")
    for ch in guild.text_channels:
        if ch.name.lower() == name:
            return ch
    return fallback

def find_role(guild, text):
    rid = first_id(text)
    if rid:
        return guild.get_role(rid)
    name = (text or "").strip().lstrip("@").lower()
    return next((r for r in guild.roles if r.name.lower() == name), None)

async def safe_send(channel, content=None, **kw):
    try:
        return await channel.send(content, **kw)
    except discord.HTTPException as e:
        log.warning("send failed in %s: %s", getattr(channel, "id", "?"), e)
        return None

def ping_mentions(ping, guild, actor):
    """(text_prefix, AllowedMentions) for 'none' | 'here' | 'everyone' | @role, respecting the actor's permission."""
    ping = (ping or "none").strip().lower()
    if ping in ("", "none", "no", "off"):
        return "", discord.AllowedMentions.none()
    if ping in ("everyone", "@everyone", "here", "@here"):
        if actor is not None and not has_perm(actor, "mention_everyone"):
            return "", discord.AllowedMentions.none()
        tag = "@everyone" if "everyone" in ping else "@here"
        return tag + " ", discord.AllowedMentions(everyone=True, roles=False, users=False)
    role = find_role(guild, ping)
    if role and (role.mentionable or actor is None or has_perm(actor, "mention_everyone")):
        return role.mention + " ", discord.AllowedMentions(everyone=False, roles=[role], users=False)
    return "", discord.AllowedMentions.none()


# ---------- white-label watermark (do not remove) ----------
WATERMARK = "Made by Shine Akhanda · shineakhanda.dev"

class _BrandEmbed(discord.Embed):
    def set_footer(self, *, text=None, icon_url=None):
        text = f"{text} · {WATERMARK}" if text and WATERMARK not in text else (text or WATERMARK)
        return super().set_footer(text=text[:2048], icon_url=icon_url)

def embed(title=None, desc=None, color=COLOR):
    e = _BrandEmbed(title=title, description=desc, color=color)
    e.set_footer(text=None)
    return e
