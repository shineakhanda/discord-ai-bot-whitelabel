"""Owner's server log: every server __BOT_NAME__ is in (name, ID, members, owner, dates) with an invite link when the server
is public (vanity URL) or its admins allowed support access (/setup support_invite). Saved to data/servers.csv and
posted to OWNER_LOG_CHANNEL on join/leave."""
import os, csv, time

import discord

import store
from store import DATA, env, log
from core import client, embed, safe_send

CSV_PATH = os.path.join(DATA, "servers.csv")
LOG_CHANNEL = int(env("OWNER_LOG_CHANNEL", "0") or 0)

async def invite_for(g, create=True):
    """Public vanity link, or a support invite the server's admins allowed. Otherwise None."""
    code = getattr(g, "vanity_url_code", None)
    if code:
        return f"https://discord.gg/{code}", "public"
    s = store.guild(g.id).get("support_invite") or {}
    if not s.get("enabled"):
        return None, "not shared"
    if s.get("code"):
        try:
            await client.fetch_invite(s["code"])
            return f"https://discord.gg/{s['code']}", "support (allowed by admins)"
        except discord.HTTPException:
            pass                                        # expired or deleted → make a new one
    if not create:
        return None, "allowed, not created yet"
    me = g.me
    ch = g.system_channel if g.system_channel and g.system_channel.permissions_for(me).create_instant_invite else \
        next((c for c in g.text_channels if c.permissions_for(me).create_instant_invite), None)
    if not ch:
        return None, "allowed, but I can't create invites"
    try:
        inv = await ch.create_invite(max_age=0, max_uses=0, unique=False,
                                     reason="__BOT_NAME__ support access (allowed by this server's admins)")
    except discord.HTTPException:
        return None, "allowed, invite creation failed"
    s.update(code=inv.code)
    store.guild(g.id)["support_invite"] = s
    store.save_guild(g.id)
    return inv.url, "support (allowed by admins)"

async def info(g, create=True):
    url, kind = await invite_for(g, create)
    me = g.me
    return {"name": g.name, "id": str(g.id), "members": g.member_count or 0, "owner_id": str(g.owner_id or ""),
            "created": g.created_at.strftime("%Y-%m-%d") if g.created_at else "",
            "joined": me.joined_at.strftime("%Y-%m-%d") if me and me.joined_at else "",
            "boost_level": getattr(g, "premium_tier", 0), "tier": store.guild(g.id)["tier"],
            "invite": url or "", "invite_type": kind}

FIELDS = ["name", "id", "members", "owner_id", "created", "joined", "boost_level", "tier", "invite", "invite_type"]

async def snapshot():
    """Write data/servers.csv for all servers. Returns the rows."""
    rows = []
    for g in sorted(client.guilds, key=lambda x: -(x.member_count or 0)):
        try:
            rows.append(await info(g))
        except Exception as e:
            log.warning("server log %s: %s", g.id, e)
    tmp = CSV_PATH + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CSV_PATH)
    return rows

def _card(r, joined=True):
    e = embed(f"{'📥 Joined' if joined else '📤 Left'}: {r['name']}",
              f"ID `{r['id']}` · {r['members']} members · owner <@{r['owner_id']}> (`{r['owner_id']}`)\n"
              f"Created {r['created']} · plan {r['tier']}\n"
              + (f"Invite: {r['invite']} ({r['invite_type']})" if r.get("invite") else f"Invite: {r['invite_type']}"),
              color=0x22C55E if joined else 0xEF4444)
    e.set_footer(text=f"Now in {len(client.guilds)} servers")
    return e

async def _post(e):
    if not LOG_CHANNEL:
        return
    ch = client.get_channel(LOG_CHANNEL)
    if ch:
        await safe_send(ch, embed=e)

async def on_join(g):
    try:
        r = await info(g)
        await _post(_card(r, True))
        await snapshot()
    except Exception as e:
        log.warning("server log join: %s", e)

async def on_leave(g):
    r = {"name": g.name, "id": str(g.id), "members": g.member_count or 0, "owner_id": str(g.owner_id or ""),
         "created": g.created_at.strftime("%Y-%m-%d") if g.created_at else "", "tier": "-", "invite": "",
         "invite_type": "left"}
    await _post(_card(r, False))
    try:
        await snapshot()
    except Exception as e:
        log.warning("server log leave: %s", e)
