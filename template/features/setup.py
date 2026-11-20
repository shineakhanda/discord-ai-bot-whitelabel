"""/setup — per-server settings managed by the server's own admins."""
from typing import Literal, Optional

import discord
from discord import app_commands

import store, panel, web
from core import tree, embed, is_admin
from timeparse import valid_tz

async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

setup_grp = app_commands.Group(name="setup", description="__BOT_NAME__ settings for this server (admins)",
                               guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

async def _admin(inter):
    if not is_admin(inter.user):
        await _deny(inter, "You need **Manage Server** to change __BOT_NAME__'s settings."); return False
    return True

@setup_grp.command(name="view", description="Show all __BOT_NAME__ settings for this server")
async def setup_view(inter: discord.Interaction):
    if not await _admin(inter):
        return
    gid = inter.guild_id
    g = store.guild(gid)
    feats = " ".join(f"{'✅' if v else '❌'} {k}" for k, v in g["features"].items())
    chans = ", ".join(f"<#{c}>" for c in g["auto_channels"]) or "none"
    t = g["tickets"]
    troles = ", ".join(f"<@&{r}>" for r in t["roles"]) or "none"
    p = g["panel"]
    usage = " · ".join(f"{k} {store.used(f'g:{gid}', k)}/{store.limit_for(gid, k)}"
                       for k in ("chat", "search", "browse", "panel", "mod", "announce"))
    e = embed("⚙️ __BOT_NAME__ settings", f"Plan: **{g['tier']}** · Language: **{g['lang']}** · Timezone: **{g['timezone']}**"
              f"\nFeatures: {feats}")
    e.add_field(name="Auto-reply channels", value=chans, inline=False)
    tcat = f"<#{t['category']}>" if t["category"] else "any"
    e.add_field(name="Tickets", value=f"{'on' if t['enabled'] else 'off'} · roles: {troles} · "
                f"category: {tcat} · handoff: {t['handoff']}", inline=False)
    paccess = f"<@&{p['role']}>" if p["role"] else "admins"
    padmin = f"<@&{p['admin_role']}>" if p["admin_role"] else "Manage Server"
    plog = f"<#{p['log_channel']}>" if p["log_channel"] else "none"
    e.add_field(name="Game panel", value=(f"`{p['url']}` · {'full admin ✅' if p['app_key'] else 'client key only'} · "
                                          f"members: {paccess} · admins: {padmin} · log: {plog} · "
                                          f"auto-backup: {p['auto_backup']}") if p["url"] else "not linked",
                inline=False)
    m = g["mod"]
    mlog = f"<#{m['log_channel']}>" if m["log_channel"] else "none"
    e.add_field(name="AI admin", value=f"{'on' if m['enabled'] else 'off'} · {m['strictness']} · log: {mlog} · "
                                       f"delete {m['delete']} · timeout {m['timeout']}", inline=False)
    an = g["announce"]
    arole = f"<@&{an['role']}>" if an["role"] else "Manage Server"
    ach = f"<#{an['channel']}>" if an["channel"] else "where asked"
    e.add_field(name="Announcements", value=f"by {arole} · default {ach} · schedules: {len(store.sched_list(gid))}",
                inline=False)
    w = g["welcome"]
    e.add_field(name="Welcome", value=f"{'on' if w['enabled'] else 'off'}" + (f" in <#{w['channel']}>" if w["channel"] else ""),
                inline=False)
    e.add_field(name="Server info", value=(f"{len(g['knowledge'])} chars · used in: {g['knowledge_scope']}"
                                           if g["knowledge"] else "not set (`/setup knowledge`)"), inline=False)
    e.add_field(name="Today's usage", value=usage, inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)

@setup_grp.command(name="language", description="Reply language for this server")
async def setup_language(inter: discord.Interaction, mode: Literal["auto", "bn", "banglish", "en"]):
    if not await _admin(inter):
        return
    store.guild(inter.guild_id)["lang"] = mode; store.save_guild(inter.guild_id)
    await inter.response.send_message(f"✅ Language: **{mode}** (auto = match each user)", ephemeral=True)

@setup_grp.command(name="autochannel", description="Toggle: __BOT_NAME__ answers every message in a channel")
async def setup_autochannel(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await _admin(inter):
        return
    ch = channel or inter.channel
    g = store.guild(inter.guild_id)
    if ch.id in g["auto_channels"]:
        g["auto_channels"].remove(ch.id); txt = f"❌ Auto-reply OFF in {ch.mention}"
    else:
        if len(g["auto_channels"]) >= 5:
            return await _deny(inter, "Max 5 auto-reply channels per server.")
        g["auto_channels"].append(ch.id); txt = f"✅ Auto-reply ON in {ch.mention}"
    store.save_guild(inter.guild_id)
    await inter.response.send_message(txt, ephemeral=True)

@setup_grp.command(name="feature", description="Turn a __BOT_NAME__ feature on or off for this server")
async def setup_feature(inter: discord.Interaction,
                        name: Literal["chat", "search", "browse", "panel", "tickets", "moderation"],
                        enabled: bool):
    if not await _admin(inter):
        return
    store.guild(inter.guild_id)["features"][name] = enabled; store.save_guild(inter.guild_id)
    await inter.response.send_message(f"{'✅' if enabled else '❌'} {name} {'on' if enabled else 'off'}", ephemeral=True)

class KnowledgeModal(discord.ui.Modal, title="Server info for __BOT_NAME__"):
    text = discord.ui.TextInput(label="Rules, IP, FAQ, links, prices...", style=discord.TextStyle.paragraph,
                                max_length=4000, required=False)

    def __init__(self, gid, scope):
        super().__init__()
        self.gid, self.scope = gid, scope
        self.text.default = store.guild(gid)["knowledge"]

    async def on_submit(self, inter: discord.Interaction):
        g = store.guild(self.gid)
        g["knowledge"] = str(self.text.value).strip()
        if self.scope:
            g["knowledge_scope"] = self.scope
        store.save_guild(self.gid)
        await inter.response.send_message(f"✅ Saved {len(g['knowledge'])} chars of server info "
                                          f"(used in: {g['knowledge_scope']}).", ephemeral=True)

@setup_grp.command(name="knowledge", description="Teach __BOT_NAME__ about your server (rules, IP, FAQ)")
@app_commands.describe(scope="Use it in all chats, or only inside tickets (saves tokens)")
async def setup_knowledge(inter: discord.Interaction, scope: Optional[Literal["all", "tickets"]] = None):
    if not await _admin(inter):
        return
    await inter.response.send_modal(KnowledgeModal(inter.guild_id, scope))

@setup_grp.command(name="infochannels", description="Channels __BOT_NAME__ always reads (pricing, plans, rules, FAQ)")
@app_commands.describe(action="add / remove a channel, or list them", channel="The channel (e.g. #pricing)")
async def setup_infochannels(inter: discord.Interaction, action: Literal["add", "remove", "list"],
                             channel: Optional[discord.TextChannel] = None):
    if not await _admin(inter):
        return
    import agent
    g = store.guild(inter.guild_id)
    ids = g.setdefault("info_channels", [])
    if action == "list" or channel is None:
        txt = "\n".join(f"• <#{c}>" for c in ids) or "None yet. Add one: `/setup infochannels add #pricing`"
        return await inter.response.send_message(f"**Info channels** (__BOT_NAME__ always knows what's in them):\n{txt}",
                                                 ephemeral=True)
    if action == "add":
        if channel.id not in ids:
            if len(ids) >= 15:
                return await inter.response.send_message("Max 15 info channels. Remove one first.", ephemeral=True)
            me = inter.guild.me
            if not (channel.permissions_for(me).view_channel and channel.permissions_for(me).read_message_history):
                return await inter.response.send_message(
                    f"I can't read {channel.mention}. Give me **View Channel** + **Read Message History** there.",
                    ephemeral=True)
            ids.append(channel.id)
        msg = f"✅ I'll always read {channel.mention} (refreshed every few minutes)."
    else:
        if channel.id in ids:
            ids.remove(channel.id)
        msg = f"✅ Removed {channel.mention}."
    agent.info_invalidate(channel.id)
    store.save_guild(inter.guild_id)
    await inter.response.send_message(msg, ephemeral=True)

@setup_grp.command(name="tickets", description="AI first-line support inside tickets from your ticket bot")
@app_commands.describe(enabled="Turn ticket support on/off", role="Support role your ticket bot pings (toggle add/remove)",
                       category="Only treat channels in this category as tickets (recommended)",
                       handoff="Step back when staff reply")
async def setup_tickets(inter: discord.Interaction, enabled: bool, role: Optional[discord.Role] = None,
                        category: Optional[discord.CategoryChannel] = None, handoff: Optional[bool] = None):
    if not await _admin(inter):
        return
    g = store.guild(inter.guild_id)
    t = g["tickets"]
    t["enabled"] = enabled
    if role:
        if role.id in t["roles"]:
            t["roles"].remove(role.id)
        elif len(t["roles"]) < 5:
            t["roles"].append(role.id)
    if category:
        t["category"] = category.id
    if handoff is not None:
        t["handoff"] = handoff
    store.save_guild(inter.guild_id)
    roles = ", ".join(f"<@&{r}>" for r in t["roles"]) or "⚠️ none yet: add the role your ticket bot pings"
    tcat = f"<#{t['category']}>" if t["category"] else "any (only bot-created ticket messages)"
    await inter.response.send_message(
        f"🎫 Tickets **{'on' if enabled else 'off'}** · roles: {roles} · category: {tcat} · "
        f"handoff: {t['handoff']}\n-# I reply automatically when your ticket bot opens a ticket: either its "
        f"message pings one of these roles (text or embed), or — if you set the ticket category — any new ticket "
        f"channel in that category (no ping needed). Make sure I can see the ticket channels.", ephemeral=True)

class PanelModal(discord.ui.Modal, title="Link Pterodactyl / Reviactyl panel"):
    url = discord.ui.TextInput(label="Panel URL", placeholder="https://panel.example.com", max_length=200)
    key = discord.ui.TextInput(label="Client API key (ptlc_…) from an admin account",
                               placeholder="Account → API Credentials", max_length=200)
    app_key = discord.ui.TextInput(label="Application API key (ptla_…) — optional",
                                   placeholder="Admin → Application API (egg change, limits, suspend)",
                                   max_length=200, required=False)

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        url = panel.normalize_url(str(self.url.value))
        key, app_key = str(self.key.value).strip(), str(self.app_key.value or "").strip()
        try:
            await panel.test_key(url, key)
            if app_key:
                await panel.test_key(url, app_key, app_key=True)
        except (panel.PanelError, web.BlockedURL) as e:
            return await inter.followup.send(f"⚠️ Couldn't link: {e}", ephemeral=True)
        p = store.guild(inter.guild_id)["panel"]
        p["url"], p["key"] = url, store.encrypt(key)
        p["app_key"] = store.encrypt(app_key) if app_key else ""
        store.save_guild(inter.guild_id)
        panel.clear_cache(inter.guild_id)
        await inter.followup.send(
            f"✅ Panel linked: `{url}` (keys stored encrypted){' with full admin (application key)' if app_key else ''}.\n"
            "Next: `/setup panel_access` to choose who can use it and a log channel. Admins can now just ask me "
            "things like *\"SMP won't start, fix it\"* or *\"change SMP to 1.21.1\"*.", ephemeral=True)

@setup_grp.command(name="panel", description="Link a Pterodactyl/Pelican panel with a client API key")
async def setup_panel(inter: discord.Interaction):
    if not await _admin(inter):
        return
    await inter.response.send_modal(PanelModal())

@setup_grp.command(name="panel_access", description="Who can use the game panel, admin role, log channel")
@app_commands.describe(role="Members who can view/power servers", admin_role="Can make __BOT_NAME__ fix/reinstall/change servers "
                       "(Manage Server always can)", log_channel="Where __BOT_NAME__ logs every panel action",
                       auto_backup="Back up before reinstall / egg change", console="Allow /panel console",
                       owner_role="Hosting owners: manage panel users, all servers, nodes (Administrator always can)")
async def setup_panel_access(inter: discord.Interaction, role: Optional[discord.Role] = None,
                             admin_role: Optional[discord.Role] = None, owner_role: Optional[discord.Role] = None,
                             log_channel: Optional[discord.TextChannel] = None,
                             auto_backup: Optional[bool] = None, console: Optional[bool] = None):
    if not await _admin(inter):
        return
    p = store.guild(inter.guild_id)["panel"]
    if role is not None:
        p["role"] = role.id
    if admin_role is not None:
        p["admin_role"] = admin_role.id
    if owner_role is not None:
        p["owner_role"] = owner_role.id
    if log_channel is not None:
        p["log_channel"] = log_channel.id
    if auto_backup is not None:
        p["auto_backup"] = auto_backup
    if console is not None:
        p["console"] = console
    store.save_guild(inter.guild_id)
    members = f"<@&{p['role']}>" if p["role"] else "admins only"
    admins = f"<@&{p['admin_role']}>" if p["admin_role"] else "Manage Server"
    logch = f"<#{p['log_channel']}>" if p["log_channel"] else "none"
    await inter.response.send_message(f"✅ Members: {members} · Panel admins: {admins} · Log: {logch} · "
                                      f"Auto-backup: {p['auto_backup']} · Console: {p['console']}", ephemeral=True)

@setup_grp.command(name="panel_watch", description="Every 15 min: scan the whole panel for miners and CPU hogs")
@app_commands.describe(enabled="Needs the Application API key and a panel log channel",
                       cpu_percent="Alert when a server uses this % of its CPU limit for 30+ minutes",
                       auto_suspend="Automatically suspend servers with crypto-miner signs")
async def setup_panel_watch(inter: discord.Interaction, enabled: bool,
                            cpu_percent: Optional[app_commands.Range[int, 50, 1000]] = None,
                            auto_suspend: Optional[bool] = None):
    if not await _admin(inter):
        return
    p = store.guild(inter.guild_id)["panel"]
    p["watch"] = enabled
    if cpu_percent is not None:
        p["watch_cpu"] = cpu_percent
    if auto_suspend is not None:
        p["watch_suspend"] = auto_suspend
    store.save_guild(inter.guild_id)
    need = []
    if not p["app_key"]:
        need.append("an Application API key (`/setup panel`)")
    if not p["log_channel"]:
        need.append("a log channel (`/setup panel_access log_channel:`)")
    await inter.response.send_message(f"🕵️ Panel watch **{'on' if enabled else 'off'}** · CPU alert at "
                                      f"{p['watch_cpu']}% · auto-suspend miners: {p['watch_suspend']}"
                                      + (f"\n⚠️ Still needs {' and '.join(need)}." if need else ""), ephemeral=True)

@setup_grp.command(name="panel_unlink", description="Remove the linked panel and its key")
async def setup_panel_unlink(inter: discord.Interaction):
    if not await _admin(inter):
        return
    store.guild(inter.guild_id)["panel"].update(url="", key="", app_key="", role=None, admin_role=None,
                                                 console=False)
    store.save_guild(inter.guild_id)
    panel.clear_cache(inter.guild_id)
    await inter.response.send_message("🗑️ Panel unlinked and key deleted.", ephemeral=True)

@setup_grp.command(name="reset", description="Reset all __BOT_NAME__ settings for this server")
async def setup_reset(inter: discord.Interaction, confirm: Literal["yes"]):
    if not await _admin(inter):
        return
    tier = store.guild(inter.guild_id)["tier"]
    store.delete_guild(inter.guild_id)
    if tier != "free":
        store.guild(inter.guild_id)["tier"] = tier; store.save_guild(inter.guild_id)
    panel.clear_cache(inter.guild_id)
    await inter.response.send_message("♻️ Server settings reset to defaults.", ephemeral=True)


@setup_grp.command(name="timezone", description="Timezone for schedules and reminders (e.g. Asia/Dhaka, Europe/London)")
async def setup_timezone(inter: discord.Interaction, timezone: str):
    if not await _admin(inter):
        return
    if not valid_tz(timezone):
        return await _deny(inter, "Unknown timezone. Use names like `Asia/Dhaka`, `Asia/Kolkata`, `Europe/London`, `UTC`.")
    store.guild(inter.guild_id)["timezone"] = timezone; store.save_guild(inter.guild_id)
    await inter.response.send_message(f"🕒 Timezone: **{timezone}**", ephemeral=True)

@setup_grp.command(name="announcements", description="Who can make __BOT_NAME__ announce, and the default channel")
async def setup_announcements(inter: discord.Interaction, role: Optional[discord.Role] = None,
                              channel: Optional[discord.TextChannel] = None):
    if not await _admin(inter):
        return
    a = store.guild(inter.guild_id)["announce"]
    if role is not None:
        a["role"] = role.id
    if channel is not None:
        a["channel"] = channel.id
    store.save_guild(inter.guild_id)
    r = f"<@&{a['role']}>" if a["role"] else "Manage Server only"
    c = f"<#{a['channel']}>" if a["channel"] else "the channel where asked"
    await inter.response.send_message(f"📢 Announcers: {r} · Default channel: {c}", ephemeral=True)

@setup_grp.command(name="moderation", description="__BOT_NAME__ AI admin: watch chat, warn, timeout, report to staff")
@app_commands.describe(enabled="Turn the AI admin on/off", log_channel="Staff channel for reports",
                       mod_role="Mod role to ping on serious cases (toggle add/remove)",
                       strictness="low = only obvious spam/scams, high = stricter on insults",
                       delete="Delete clear violations", timeout="Auto-timeout after repeated warnings",
                       invites="Allow Discord invite links", exempt_role="Role __BOT_NAME__ never moderates (toggle)",
                       exempt_channel="Channel __BOT_NAME__ never moderates (toggle)", dm_warn="Also DM warnings")
async def setup_moderation(inter: discord.Interaction, enabled: bool,
                           log_channel: Optional[discord.TextChannel] = None, mod_role: Optional[discord.Role] = None,
                           strictness: Optional[Literal["low", "medium", "high"]] = None,
                           delete: Optional[bool] = None, timeout: Optional[bool] = None,
                           invites: Optional[bool] = None, exempt_role: Optional[discord.Role] = None,
                           exempt_channel: Optional[discord.TextChannel] = None, dm_warn: Optional[bool] = None):
    if not await _admin(inter):
        return
    m = store.guild(inter.guild_id)["mod"]
    m["enabled"] = enabled
    if log_channel is not None:
        m["log_channel"] = log_channel.id
    for key, item in (("mod_roles", mod_role), ("exempt_roles", exempt_role), ("exempt_channels", exempt_channel)):
        if item is not None:
            (m[key].remove if item.id in m[key] else m[key].append)(item.id)
    for key, val in (("strictness", strictness), ("delete", delete), ("timeout", timeout), ("invites", invites),
                     ("dm_warn", dm_warn)):
        if val is not None:
            m[key] = val
    store.save_guild(inter.guild_id)
    warn = "" if m["log_channel"] else "\n⚠️ Set a `log_channel` so staff get reports."
    await inter.response.send_message(
        f"🛡️ AI admin **{'on' if enabled else 'off'}** · strictness {m['strictness']} · delete {m['delete']} · "
        f"timeout {m['timeout']} · invites allowed {m['invites']} · mod roles "
        f"{', '.join(f'<@&{r}>' for r in m['mod_roles']) or 'none'}{warn}\n"
        "-# Needs my permissions: Manage Messages (delete), Moderate Members (timeout), and my role above members.",
        ephemeral=True)

@setup_grp.command(name="automod", description="Extra auto-mod: links, raids, new accounts, daily report")
@app_commands.describe(links="allow all, block all, or allow only listed domains",
                       allow_domains="Comma list for allowlist mode, e.g. youtube.com,github.com",
                       raid_joins="Joins per minute that count as a raid (needs MEMBERS_INTENT)",
                       raid_action="alert mods, or also raise verification to High",
                       min_account_days="Flag accounts younger than this many days (0 = off)",
                       new_account_action="alert mods, or kick young accounts",
                       digest="Daily AI moderation report in the log channel")
async def setup_automod(inter: discord.Interaction, links: Optional[Literal["allow", "block", "allowlist"]] = None,
                        allow_domains: Optional[str] = None, raid_joins: Optional[app_commands.Range[int, 3, 100]] = None,
                        raid_action: Optional[Literal["alert", "lock"]] = None,
                        min_account_days: Optional[app_commands.Range[int, 0, 365]] = None,
                        new_account_action: Optional[Literal["alert", "kick"]] = None, digest: Optional[bool] = None):
    if not await _admin(inter):
        return
    m = store.guild(inter.guild_id)["mod"]
    if allow_domains is not None:
        m["allow_domains"] = [d.strip().lower().removeprefix("www.") for d in allow_domains.split(",") if d.strip()][:50]
    for key, val in (("links", links), ("raid_joins", raid_joins), ("raid_action", raid_action),
                     ("min_age_days", min_account_days), ("new_account_action", new_account_action), ("digest", digest)):
        if val is not None:
            m[key] = val
    store.save_guild(inter.guild_id)
    from core import intents
    tip = "" if intents.members else "\n-# Raid and new-account checks need the bot owner to enable MEMBERS_INTENT."
    await inter.response.send_message(
        f"🛡️ Links: **{m['links']}**" + (f" ({', '.join(m['allow_domains'][:8])})" if m["links"] == "allowlist" else "")
        + f" · Raid: {m['raid_joins']}/min → {m['raid_action']} · New accounts < {m['min_age_days']}d → "
        f"{m['new_account_action']} · Daily report: {m['digest']}{tip}", ephemeral=True)

@setup_grp.command(name="badwords", description="Add/remove extra words __BOT_NAME__ should check (comma separated)")
async def setup_badwords(inter: discord.Interaction, add: Optional[str] = None, remove: Optional[str] = None):
    if not await _admin(inter):
        return
    m = store.guild(inter.guild_id)["mod"]
    for w in (add or "").split(","):
        w = w.strip().lower()
        if w and w not in m["words"] and len(m["words"]) < 100:
            m["words"].append(w)
    for w in (remove or "").split(","):
        w = w.strip().lower()
        if w in m["words"]:
            m["words"].remove(w)
    store.save_guild(inter.guild_id)
    await inter.response.send_message(f"📝 {len(m['words'])} custom words.", ephemeral=True)

@setup_grp.command(name="welcome", description="AI welcome messages for new members (needs Server Members intent)")
@app_commands.describe(note="Extra info for the welcome, e.g. 'read #rules, get roles in #roles'")
async def setup_welcome(inter: discord.Interaction, enabled: bool, channel: Optional[discord.TextChannel] = None,
                        note: Optional[app_commands.Range[str, 0, 300]] = None):
    if not await _admin(inter):
        return
    w = store.guild(inter.guild_id)["welcome"]
    w["enabled"] = enabled
    if channel is not None:
        w["channel"] = channel.id
    if note is not None:
        w["note"] = note
    store.save_guild(inter.guild_id)
    from core import intents
    tip = "" if intents.members else "\n⚠️ The bot owner must enable Server Members intent (MEMBERS_INTENT=1) for this."
    where = f" in <#{w['channel']}>" if w["channel"] else " (pick a channel)"
    await inter.response.send_message(f"👋 Welcome {'on' if enabled else 'off'}{where}{tip}", ephemeral=True)

@setup_grp.command(name="support_invite", description="Allow __BOT_NAME__'s developer to get an invite here for support")
@app_commands.describe(enabled="If on, __BOT_NAME__ creates an invite that only the bot's developer sees, so they can help you")
async def setup_support_invite(inter: discord.Interaction, enabled: bool):
    if not await _admin(inter):
        return
    si = store.guild(inter.guild_id).setdefault("support_invite", {"enabled": False, "code": ""})
    old = si.get("code")
    si["enabled"] = enabled
    if not enabled and old:
        try:
            inv = await inter.client.fetch_invite(old)
            await inv.delete(reason="support access turned off by admin")
        except discord.HTTPException:
            pass
        si["code"] = ""
    store.save_guild(inter.guild_id)
    await inter.response.send_message(
        "🤝 Support access **on**: __BOT_NAME__'s developer can join to help when you need it. Turn it off anytime."
        if enabled else "🔒 Support access **off** (any support invite I made was deleted).", ephemeral=True)

tree.add_command(setup_grp)
