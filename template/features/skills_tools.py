"""Tools ported from OpenClaw-style skills: weather, and teaching the bot new skills from Discord."""
import re
from urllib.parse import quote

import aiohttp

import store, workspace
from store import HTTP, log
from agent import tool, Result


@tool("weather", "weather | <place: city, region, airport code or coordinates> | <days 0-3, optional>",
      "current weather and up to 3-day forecast for a place (wttr.in)", progress="☔ Checking the weather in **{0}**…")
async def t_weather(c, a):
    place = (a[0] if a else "").strip() or "Dhaka"
    days = max(0, min(3, int(re.sub(r"\D", "", a[1]) or 0))) if len(a) > 1 and a[1] else 0
    try:
        async with HTTP["s"].get(f"https://wttr.in/{quote(place)}?format=j1",
                                 headers={"User-Agent": "curl/8", "Accept-Language": "en"},
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return Result(f"Weather service returned {r.status} for {place}.")
            d = await r.json(content_type=None)
    except Exception as e:
        return Result(f"Couldn't get the weather: {e}")
    cur = (d.get("current_condition") or [{}])[0]
    area = (d.get("nearest_area") or [{}])[0]
    where = ", ".join(x[0]["value"] for x in (area.get("areaName"), area.get("country")) if x) or place
    lines = [f"Weather for {where} (now): {cur.get('weatherDesc', [{}])[0].get('value', '?')}, "
             f"{cur.get('temp_C')}°C (feels {cur.get('FeelsLikeC')}°C), humidity {cur.get('humidity')}%, "
             f"wind {cur.get('windspeedKmph')} km/h, precipitation {cur.get('precipMM')} mm."]
    for w in (d.get("weather") or [])[:max(1, days or 1)]:
        hourly = w.get("hourly") or []
        rain = max((int(h.get("chanceofrain", 0) or 0) for h in hourly), default=0)
        desc = hourly[len(hourly) // 2]["weatherDesc"][0]["value"] if hourly else ""
        lines.append(f"{w.get('date')}: {desc}, {w.get('mintempC')}–{w.get('maxtempC')}°C, rain chance up to {rain}%.")
    return Result("\n".join(lines), f"☔ weather {where[:30]}")


def _owner(c):
    """Skills apply in every server the bot is in, so only the bot owner may teach or remove them."""
    from core import is_owner
    return is_owner(c.user)


@tool("create_skill", "create_skill | <name> | <one-line description: what + when> | <markdown body: steps, tools, rules>",
      "(bot owner) teach the bot a new reusable skill/playbook, or replace one with the same name", when=_owner,
      progress="📝 Writing the skill **{0}**…")
async def t_create_skill(c, a):
    if len(a) < 3 or not a[0].strip() or len(a[2].strip()) < 20:
        return Result("Need: name | description | body (at least a few steps).")
    try:
        slug = workspace.save_skill(a[0], a[1], " | ".join(a[2:]))
    except Exception as e:
        return Result(f"Couldn't save the skill: {e}")
    log.info("skill %s created by %s", slug, c.user)
    return Result(f"Skill '{slug}' saved. It now appears in your skills list and you'll load it for: {a[1][:200]}",
                  f"📝 skill “{slug}” saved")


@tool("delete_skill", "delete_skill | <name>", "(bot owner) remove a skill taught from Discord", when=_owner,
      progress="🧹 Removing the skill…")
async def t_delete_skill(c, a):
    ok = workspace.delete_skill(a[0] if a else "")
    return Result("Skill deleted." if ok else "No taught skill with that name (built-in skills can't be deleted).",
                  "🧹 skill deleted" if ok else None)
