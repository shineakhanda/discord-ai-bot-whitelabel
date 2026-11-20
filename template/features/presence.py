"""Bot status: streaming (purple 'LIVE' badge) or playing/watching/listening, with rotating texts."""
import asyncio

import discord

from store import cfg, log
from core import client, on_startup

_i = {"n": 0}

def activity(text):
    p = cfg["presence"]
    kind = p.get("type", "streaming")
    text = text.replace("{servers}", str(len(client.guilds))).replace(
        "{users}", str(sum((g.member_count or 0) for g in client.guilds)))
    if kind == "streaming":
        # Discord only shows the purple "Streaming" status for twitch.tv or youtube.com links
        return discord.Streaming(name=text, url=p.get("url") or "__STREAM_URL__")
    kinds = {"playing": discord.ActivityType.playing, "watching": discord.ActivityType.watching,
             "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing}
    if kind in kinds:
        return discord.Activity(type=kinds[kind], name=text)
    return None

async def apply(step=False):
    p = cfg["presence"]
    texts = p.get("texts") or ["/help"]
    if step:
        _i["n"] = (_i["n"] + 1) % len(texts)
    try:
        await client.change_presence(status=discord.Status.online, activity=activity(texts[_i["n"] % len(texts)]))
    except Exception as e:
        log.warning("presence update failed: %s", e)

@on_startup
async def presence_loop():
    await client.wait_until_ready()
    await apply()
    while not client.is_closed():
        await asyncio.sleep(max(60, int(cfg["presence"].get("every", 300))))
        await apply(step=True)
