"""/setup type: tell __BOT_NAME__ what kind of server this is (community, support, store, gaming, Minecraft SMP, hosting)."""
from typing import Literal

import discord
from discord import app_commands

import store
from core import embed, is_admin
from features.setup import setup_grp
from servertypes import TYPES

@setup_grp.command(name="type", description="What kind of server is this? __BOT_NAME__ adapts how it talks and helps")
@app_commands.describe(kind="Pick the closest one")
async def setup_type(inter: discord.Interaction,
                     kind: Literal["general", "community", "support", "store", "game", "minecraft", "hosting"]):
    if not is_admin(inter.user):
        return await inter.response.send_message("You need **Manage Server** for this.", ephemeral=True)
    g = store.guild(inter.guild_id)
    g["type"] = kind
    store.save_guild(inter.guild_id)
    t = TYPES[kind]
    e = embed(f"✅ Server type: {t['label']}",
              (t["focus"] + "\n\n" if t["focus"] else "") + "**Recommended setup:**\n"
              + "\n".join(f"• {x}" for x in t["setup"]))
    e.set_footer(text="Change any time with /setup type")
    await inter.response.send_message(embed=e, ephemeral=True)
