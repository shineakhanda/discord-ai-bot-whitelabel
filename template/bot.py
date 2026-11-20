"""
__BOT_NAME__ — Discord AI agent owned by __OWNER__.

Structure (OpenClaw / Hermes style):
  workspace/   SOUL.md · IDENTITY.md · AGENTS.md · TOOLS.md · MEMORY.md · skills/*/SKILL.md · memory/ (daily notes)
  agent.py     tool registry + think → act → observe loop
  features/    chat · tickets · panel_tools · announce · moderation · setup · owner
  core.py      Discord client + helpers · store.py data · ai.py LLM · web.py search/browser · panel.py panel API
Start: python bot.py
"""
print("[__BOT_NAME__] starting...", flush=True)

import discord
from discord import app_commands

import store
from store import OWNER_ID, log

_missing = [k for k, v in (("DISCORD_TOKEN", store.DISCORD_TOKEN),
                           ("NUMES_API_KEY, AI_API_KEY or OPENROUTER_API_KEY", store.AI_API_KEY if store.AI_BASE_URL else store.OR_KEY),
                           ("OWNER_ID", OWNER_ID)) if not v]
if _missing:
    raise SystemExit(f"[__BOT_NAME__] Missing in .env: {', '.join(_missing)} (is the file named exactly .env?)")

from core import client, tree
import agent                                    # built-in tools
from features import panel_tools, panel_owner, announce, moderation, tickets, setup, chat, owner, presence, serverlog, ssh_tools, activities, webdata, profiles  # noqa: F401

log.info("tools loaded: %d · skills: %s", len(agent.TOOLS), ", ".join(__import__("workspace").skills()))

@tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    log.error("slash error in /%s: %s", inter.command.name if inter.command else "?", error)
    try:
        if inter.response.is_done():
            await inter.followup.send("⚠️ Something went wrong. Try again.", ephemeral=True)
        else:
            await inter.response.send_message("⚠️ Something went wrong. Try again.", ephemeral=True)
    except discord.HTTPException:
        pass

if __name__ == "__main__":
    try:
        client.run(store.DISCORD_TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("[__BOT_NAME__] Enable MESSAGE CONTENT (and SERVER MEMBERS if MEMBERS_INTENT=1) intents in the "
                         "Discord Developer Portal → Bot.")
    except KeyboardInterrupt:
        pass
