"""Disclosed self-registration (licence / usage check).

If (and only if) a REGISTER_URL is set, the bot reports its own identity to that URL on startup and every few hours:
its Discord application ID and name, its owner ID, and the servers it's in. This is how the author of a white-label
build keeps a central, private list of who's running their code.

It is NOT hidden from the operator: the README documents it, every report is written to the log
("📡 registered …"), and it does nothing at all when REGISTER_URL is empty (so the real __BOT_NAME__ stays silent).
The destination is private to whoever owns it; the fact that reporting happens is open. We don't build covert
phone-home that hides from the person running the bot.
"""
import asyncio, json, os, time

import aiohttp

import store
from store import log, BASE, DATA, env
from core import client, on_startup

INTERVAL = int(env("REGISTER_INTERVAL_HOURS", "6") or 6) * 3600


def _url():
    u = env("REGISTER_URL", "")
    if u:
        return u.strip()
    for p in (os.path.join(BASE, "register.json"), os.path.join(DATA, "register.json")):
        try:
            with open(p, encoding="utf-8") as f:
                return (json.load(f).get("url") or "").strip()
        except Exception:
            pass
    return ""


def _version():
    v = env("BOT_VERSION", "")
    if v:
        return v
    try:                                        # short git commit, if this is a git checkout
        head = open(os.path.join(BASE, ".git", "HEAD"), encoding="utf-8").read().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return open(os.path.join(BASE, ".git", ref), encoding="utf-8").read().strip()[:10]
        return head[:10]
    except Exception:
        return ""


def _payload():
    u = client.user
    guilds = [{"id": str(g.id), "name": g.name, "members": g.member_count} for g in client.guilds]
    return {
        "app_id": str(u.id) if u else None,            # the bot's Discord application id
        "bot_name": (u.name if u else None),
        "owner_id": env("OWNER_ID", ""),
        "brand": os.environ.get("BOT_NAME") or (u.name if u else ""),
        "branded": bool(os.environ.get("BRANDED")),
        "focus": env("BOT_FOCUS", "all"),
        "guild_count": len(guilds),
        "guilds": guilds[:500],
        "version": _version(),
        "ts": int(time.time()),
    }


def _is_webhook(url):
    return "discord.com/api/webhooks/" in url or "discordapp.com/api/webhooks/" in url


async def _send(url, data):
    s = store.HTTP.get("s")
    close = False
    if s is None:
        s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        close = True
    try:
        if _is_webhook(url):                           # readable message in the owner's private channel
            g_lines = "\n".join(f"• {g['name']} — {g['members']} members (`{g['id']}`)" for g in data["guilds"][:25])
            embed = {"title": f"🤖 {data['bot_name']} online",
                     "description": f"App ID `{data['app_id']}` · {data['guild_count']} server(s)"
                                    + (f" · v{data['version']}" if data['version'] else ""),
                     "fields": [{"name": "Servers", "value": g_lines[:1024] or "none"}],
                     "color": 0x5865F2, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}
            body = {"content": None, "username": data["bot_name"] or "bot-register", "embeds": [embed]}
        else:
            body = data
        async with s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return r.status
    finally:
        if close:
            await s.close()


@on_startup
async def register_loop():
    url = _url()
    if not url:
        return                                         # no endpoint set → do nothing (real __BOT_NAME__ path)
    await client.wait_until_ready()
    host = url.split("/")[2] if "//" in url else url[:30]
    while not client.is_closed():
        try:
            data = _payload()
            status = await _send(url, data)
            if 200 <= status < 300:
                log.info("📡 registered as %s (%s) → %s · %d server(s)",
                         data["bot_name"], data["app_id"], host, data["guild_count"])
            else:
                log.warning("registration to %s returned %s", host, status)
        except Exception as e:
            log.info("registration failed: %s", str(e)[:120])
        await asyncio.sleep(INTERVAL)
