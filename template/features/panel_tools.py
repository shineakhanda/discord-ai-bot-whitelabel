"""Game panel: AI tools (member + full admin), /panel commands, power buttons, audit log."""
import time, asyncio
from typing import Literal

import discord
from discord import app_commands

import store, panel, workspace
from core import tree, embed, feature, can_panel, can_panel_admin, is_admin, safe_send, log
from agent import tool, Result

# ---------- access ----------
def _linked(c):
    return bool(c.gid and feature(c.gid, "panel") and store.guild(c.gid)["panel"]["url"])

def member_ok(c):
    return _linked(c) and can_panel(c.user, c.gid)

def admin_ok(c):
    return _linked(c) and can_panel_admin(c.user, c.gid)

def app_ok(c):
    return admin_ok(c) and panel.has_app(c.gid)

def _quota(c):
    return store.quota(c.gid, c.user.id, "panel")

async def audit(gid, user, action, server=""):
    line = f"[panel] {getattr(user, 'display_name', user)} ({getattr(user, 'id', '?')}) → {action}" + \
           (f" on {server}" if server else "")
    workspace.note(f"guild {gid}: {line}")
    log.info("guild %s %s", gid, line)
    ch_id = store.guild(gid)["panel"]["log_channel"]
    if ch_id:
        from core import client
        ch = client.get_channel(ch_id)
        if ch:
            await safe_send(ch, embed=embed(desc=f"🛠️ **{action}**" + (f" · `{server}`" if server else "")
                                            + f"\n-# by {getattr(user, 'mention', user)}"))

async def _srv(c, name):
    if not _quota(c):
        raise panel.PanelError("Today's panel limit for this server is used up.")
    return await panel.resolve(c.gid, name)

def _wrap(fn):
    async def inner(c, a):
        try:
            return await fn(c, a)
        except panel.PanelError as e:
            return Result(f"Panel: {e}")
    return inner

# ---------- member tools ----------
@tool("panel_list", "panel_list", "list game servers on this server's panel", when=member_ok,
      progress="🎮 Listing game servers…")
@_wrap
async def t_list(c, a):
    servers = await panel.list_servers(c.gid, fresh=True)
    return Result("Servers: " + ("; ".join(f"{n} [{i}]" for i, n in servers[:60]) or "none"), "🎮 listed servers")

@tool("server_status", "server_status | <server>", "live state, CPU, RAM, disk, uptime, address of a game server",
      when=member_ok, progress="🎮 Checking **{0}**…")
@_wrap
async def t_status(c, a):
    ident, name = await _srv(c, a[0])
    r = await panel.resources(c.gid, ident)
    d = await panel.details(c.gid, ident)
    return Result(f"{name} [{ident}]: state={r['state']}{' SUSPENDED' if r['suspended'] else ''}, "
                  f"cpu={r['cpu']:.1f}%/{d['cpu'] or '∞'}%, ram={panel.fmt_bytes(r['mem'])}/{d['mem'] or '∞'}MB, "
                  f"disk={panel.fmt_bytes(r['disk'])}/{d['disk'] or '∞'}MB, uptime={panel.fmt_uptime(r['uptime'])}, "
                  f"address={d['address'] or 'n/a'}, installing={d['installing']}", f"🎮 checked {name}")

@tool("panel_power", "panel_power | <server> | start/stop/restart/kill", "power a game server", when=member_ok,
      progress="⚡ Sending power action…", heavy=True)
@_wrap
async def t_power(c, a):
    if len(a) < 2:
        return Result("Format: panel_power | server | start/stop/restart/kill")
    ident, name = await _srv(c, a[0])
    sig = a[1].lower().strip()
    await panel.power(c.gid, ident, sig)
    await audit(c.gid, c.user, f"power {sig}", name)
    await asyncio.sleep(3)
    r = await panel.resources(c.gid, ident)
    return Result(f"Sent {sig} to {name}. State now: {r['state']}.", f"⚡ {sig} {name}")

# ---------- admin tools ----------
@tool("panel_logs", "panel_logs | <server>", "read the latest console output (to diagnose crashes / startup issues)",
      when=admin_ok, progress="📜 Reading console of **{0}**…", heavy=True)
@_wrap
async def t_logs(c, a):
    ident, name = await _srv(c, a[0])
    status, lines = await panel.console_logs(c.gid, ident)
    text = "\n".join(lines)[-5000:] or "(console empty — server may be offline; try starting it or read logs/latest.log)"
    return Result(f"Console of {name} (status {status}):\n{text}", f"📜 read {name} console")

@tool("panel_files", "panel_files | <server> | <folder, default />", "list files in a server folder", when=admin_ok,
      progress="📁 Listing files…", heavy=True)
@_wrap
async def t_files(c, a):
    ident, name = await _srv(c, a[0])
    files = await panel.list_files(c.gid, ident, a[1] if len(a) > 1 else "/")
    return Result("; ".join(f"{n}{'' if f else '/'}{f' ({panel.fmt_bytes(s)})' if f else ''}"
                            for n, f, s in files[:120]) or "(empty)", "📁 listed files")

@tool("panel_read", "panel_read | <server> | <path>", "read a file (configs, logs/latest.log)", when=admin_ok,
      progress="📄 Reading **{0}** file…", heavy=True)
@_wrap
async def t_read(c, a):
    if len(a) < 2:
        return Result("Format: panel_read | server | path")
    ident, name = await _srv(c, a[0])
    text = await panel.read_file(c.gid, ident, a[1])
    if a[1].endswith(".log"):
        text = text[-6000:]
    return Result(f"{a[1]} on {name}:\n{text[:6000]}", f"📄 read {a[1]}")

@tool("panel_write", "panel_write | <server> | <path> | <content>", "create/overwrite a small file (e.g. eula.txt)",
      when=admin_ok, progress="✍️ Writing file…", heavy=True)
@_wrap
async def t_write(c, a):
    from agent import split_args
    a = split_args(" | ".join(a), 3)
    if len(a) < 3:
        return Result("Format: panel_write | server | path | content")
    ident, name = await _srv(c, a[0])
    await panel.write_file(c.gid, ident, a[1], a[2].replace("\\n", "\n"))
    await audit(c.gid, c.user, f"wrote file {a[1]}", name)
    return Result(f"Wrote {a[1]} on {name}.", f"✍️ wrote {a[1]}")

@tool("panel_edit", "panel_edit | <server> | <path> | <find text> | <replace with>", "edit text in a file",
      when=admin_ok, progress="✍️ Editing file…", heavy=True)
@_wrap
async def t_edit(c, a):
    from agent import split_args
    a = split_args(" | ".join(a), 4)
    if len(a) < 4:
        return Result("Format: panel_edit | server | path | find | replace")
    ident, name = await _srv(c, a[0])
    text = await panel.read_file(c.gid, ident, a[1])
    if a[2] not in text:
        return Result(f"Text not found in {a[1]}. Read the file first and copy the exact line.")
    await panel.write_file(c.gid, ident, a[1], text.replace(a[2], a[3].replace("\\n", "\n"), 1))
    await audit(c.gid, c.user, f"edited {a[1]}", name)
    return Result(f"Edited {a[1]} on {name}.", f"✍️ edited {a[1]}")

@tool("panel_rename", "panel_rename | <server> | <from path> | <to path>", "rename/move a file (disable a plugin)",
      when=admin_ok, progress="📁 Renaming file…", heavy=True)
@_wrap
async def t_rename(c, a):
    if len(a) < 3:
        return Result("Format: panel_rename | server | from | to")
    ident, name = await _srv(c, a[0])
    await panel.rename_file(c.gid, ident, a[1], a[2])
    await audit(c.gid, c.user, f"renamed {a[1]} → {a[2]}", name)
    return Result(f"Renamed {a[1]} → {a[2]} on {name}.", "📁 renamed file")

@tool("panel_startup", "panel_startup | <server>", "startup command, variables (version etc.) and allowed Java/Docker images",
      when=admin_ok, progress="⚙️ Reading startup settings…", heavy=True)
@_wrap
async def t_startup(c, a):
    ident, name = await _srv(c, a[0])
    s = await panel.startup(c.gid, ident)
    vars_ = "; ".join(f"{v['key']}={v['value']!r}{'' if v['editable'] else ' (locked)'}"
                      f"{' rules:' + v['rules'] if v['rules'] else ''}" for v in s["vars"])
    imgs = "; ".join(f"{k} → {v}" for k, v in s["images"].items())
    extra = ""
    if panel.has_app(c.gid):
        try:
            _, srv = await panel.app_server(c.gid, ident)
            cont = srv.get("container") or {}
            extra = f"\nCurrent image: {cont.get('image')}; egg id: {srv.get('egg')}"
        except panel.PanelError:
            pass
    return Result(f"{name} startup: {s['command']}\nVariables: {vars_}\nAllowed images: {imgs}{extra}",
                  f"⚙️ read {name} startup")

@tool("panel_set_var", "panel_set_var | <server> | <VARIABLE_KEY> | <value>", "change a startup variable (version, jar…)",
      when=admin_ok, progress="⚙️ Changing startup variable…", heavy=True)
@_wrap
async def t_setvar(c, a):
    if len(a) < 3:
        return Result("Format: panel_set_var | server | KEY | value")
    ident, name = await _srv(c, a[0])
    val = await panel.set_variable(c.gid, ident, a[1].upper(), a[2])
    await audit(c.gid, c.user, f"set {a[1].upper()}={val}", name)
    return Result(f"{a[1].upper()} is now {val!r} on {name}. Reinstall if the change needs new files (versions).",
                  f"⚙️ set {a[1].upper()}")

@tool("panel_image", "panel_image | <server> | <docker image or its label, e.g. Java 21>", "change Java/Docker image",
      when=admin_ok, progress="🐳 Changing Docker image…", heavy=True)
@_wrap
async def t_image(c, a):
    if len(a) < 2:
        return Result("Format: panel_image | server | image")
    ident, name = await _srv(c, a[0])
    s = await panel.startup(c.gid, ident)
    want = a[1].strip()
    image = next((v for k, v in s["images"].items() if want.lower() in (k.lower(), v.lower())), None) or \
        next((v for k, v in s["images"].items() if want.lower() in k.lower() or want.lower() in v.lower()), None)
    if not image:
        if panel.has_app(c.gid) and "/" in want:
            image = want
        else:
            return Result(f"Image not allowed for this egg. Allowed: {', '.join(s['images']) or 'none listed'}")
    if panel.has_app(c.gid):
        sid, srv = await panel.app_server(c.gid, ident)
        cont = srv.get("container") or {}
        await panel.app(c.gid, "PATCH", f"/servers/{sid}/startup",
                        json_body={"startup": cont.get("startup_command"), "environment": cont.get("environment", {}),
                                   "egg": srv.get("egg"), "image": image, "skip_scripts": True})
    else:
        await panel.set_image(c.gid, ident, image)
    await audit(c.gid, c.user, f"docker image → {image}", name)
    return Result(f"{name} now uses {image}. Restart to apply.", "🐳 changed image")

@tool("panel_command", "panel_command | <server> | <console command>", "run a console command", when=admin_ok,
      progress="⌨️ Running console command…", heavy=True)
@_wrap
async def t_command(c, a):
    if len(a) < 2:
        return Result("Format: panel_command | server | command")
    ident, name = await _srv(c, a[0])
    await panel.command(c.gid, ident, " | ".join(a[1:]))
    await audit(c.gid, c.user, f"console: {' | '.join(a[1:])[:100]}", name)
    return Result(f"Sent to {name} console: {' | '.join(a[1:])}", "⌨️ ran command")

@tool("panel_backup", "panel_backup | <server>", "create a backup now", when=admin_ok, progress="💾 Creating backup…",
      heavy=True)
@_wrap
async def t_backup(c, a):
    ident, name = await _srv(c, a[0])
    msg = await panel.backup(c.gid, ident, "__BOT_NAME__ backup")
    await audit(c.gid, c.user, f"backup ({msg})", name)
    return Result(f"{name}: {msg}.", "💾 backup")

async def _auto_backup(c, ident):
    if not store.guild(c.gid)["panel"]["auto_backup"]:
        return "auto-backup is off"
    try:
        return await panel.backup(c.gid, ident, "__BOT_NAME__ auto-backup")
    except panel.PanelError as e:
        return f"backup failed ({e})"

@tool("panel_reinstall", "panel_reinstall | <server>", "reinstall the server (downloads files for the set version/egg)",
      when=admin_ok, progress="♻️ Reinstalling **{0}**…", heavy=True)
@_wrap
async def t_reinstall(c, a):
    ident, name = await _srv(c, a[0])
    b = await _auto_backup(c, ident)
    if b == "backup started":
        await asyncio.sleep(8)                    # give the backup a head start
    await panel.reinstall(c.gid, ident)
    await audit(c.gid, c.user, f"reinstall ({b})", name)
    return Result(f"Reinstall started for {name} ({b}). It takes 1–5 minutes; then start it and check panel_logs.",
                  f"♻️ reinstalled {name}")

@tool("panel_eggs", "panel_eggs | <optional search>", "list available eggs (server types) on the panel", when=app_ok,
      progress="🥚 Listing eggs…", heavy=True)
@_wrap
async def t_eggs(c, a):
    q = (a[0] if a else "").lower()
    eg = [e for e in await panel.eggs(c.gid) if not q or q in f"{e['nest']} {e['name']}".lower()]
    return Result("Eggs: " + "; ".join(f"#{e['id']} {e['nest']} / {e['name']}" for e in eg[:80]) or "none", "🥚 eggs")

@tool("panel_change_egg", "panel_change_egg | <server> | <egg id or name>", "switch server type/egg and reinstall",
      when=app_ok, progress="🥚 Changing egg…", heavy=True)
@_wrap
async def t_change_egg(c, a):
    if len(a) < 2:
        return Result("Format: panel_change_egg | server | egg")
    ident, name = await _srv(c, a[0])
    b = await _auto_backup(c, ident)
    if b == "backup started":
        await asyncio.sleep(8)
    egg, img = await panel.change_egg(c.gid, ident, a[1])
    await panel.reinstall(c.gid, ident)
    await audit(c.gid, c.user, f"egg → {egg['name']} (#{egg['id']}), image {img}, reinstall ({b})", name)
    return Result(f"{name} switched to egg {egg['name']} (#{egg['id']}) with {img}; reinstall started ({b}). "
                  f"Start it after install and check panel_logs.", f"🥚 egg → {egg['name']}")

@tool("panel_build", "panel_build | <server> | memory=<MB> disk=<MB> cpu=<%> swap=<MB>", "change resource limits",
      when=app_ok, progress="📈 Changing limits…", heavy=True)
@_wrap
async def t_build(c, a):
    import re
    if len(a) < 2:
        return Result("Format: panel_build | server | memory=4096 disk=20480 cpu=200")
    kv = {k.lower(): int(v) for k, v in re.findall(r"(memory|ram|disk|cpu|swap)\s*[=:]\s*(\d+)", " ".join(a[1:]), re.I)}
    if "ram" in kv:
        kv["memory"] = kv.pop("ram")
    if not kv:
        return Result("Give values like memory=4096 disk=20480 cpu=200")
    ident, name = await _srv(c, a[0])
    body = await panel.set_build(c.gid, ident, **kv)
    await audit(c.gid, c.user, f"limits → mem {body['memory']}MB, disk {body['disk']}MB, cpu {body['cpu']}%", name)
    return Result(f"{name} limits now: memory {body['memory']} MB, disk {body['disk']} MB, cpu {body['cpu']}%, "
                  f"swap {body['swap']} MB. Restart to apply.", "📈 changed limits")

@tool("panel_suspend", "panel_suspend | <server> | on/off", "suspend or unsuspend a server", when=app_ok,
      progress="⛔ Updating suspension…", heavy=True)
@_wrap
async def t_suspend(c, a):
    ident, name = await _srv(c, a[0])
    on = (a[1].lower() if len(a) > 1 else "on") in ("on", "yes", "true", "suspend")
    await panel.suspend(c.gid, ident, on)
    await audit(c.gid, c.user, "suspended" if on else "unsuspended", name)
    return Result(f"{name} {'suspended' if on else 'unsuspended'}.", "⛔ suspended" if on else "✅ unsuspended")

# =====================================================================
# /panel commands + power buttons
# =====================================================================
panel_grp = app_commands.Group(name="panel", description="Game servers on the linked Pterodactyl/Reviactyl panel",
                               guild_only=True)

async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

async def server_autocomplete(inter: discord.Interaction, current: str):
    if not can_panel(inter.user, inter.guild_id):
        return []
    try:
        servers = await panel.list_servers(inter.guild_id)
    except Exception:
        return []
    cur = current.lower()
    return [app_commands.Choice(name=n[:100], value=i) for i, n in servers if cur in n.lower() or cur in i][:25]

async def _gate(inter, admin=False):
    gid = inter.guild_id
    if not feature(gid, "panel") or not store.guild(gid)["panel"]["url"]:
        await _deny(inter, "No game panel linked here. Admins: `/setup panel`."); return False
    if not (can_panel_admin if admin else can_panel)(inter.user, gid):
        await _deny(inter, "You don't have access to the game panel here."); return False
    if not store.quota(gid, inter.user.id, "panel"):
        from core import limit_text
        await _deny(inter, limit_text("panel")); return False
    return True

async def status_embed(gid, ident):
    ident, name = await panel.resolve(gid, ident)
    r = await panel.resources(gid, ident)
    try:
        d = await panel.details(gid, ident)
    except panel.PanelError:
        d = {"mem": 0, "disk": 0, "cpu": 0, "address": ""}
    e = embed(f"{panel.STATE_ICON.get(r['state'], '⚪')} {name}",
              f"State: **{r['state']}**" + (" · ⛔ suspended" if r["suspended"] else ""))
    e.add_field(name="CPU", value=f"{r['cpu']:.1f}%" + (f" / {d['cpu']}%" if d["cpu"] else ""))
    e.add_field(name="RAM", value=panel.fmt_bytes(r["mem"]) + (f" / {d['mem']} MB" if d["mem"] else ""))
    e.add_field(name="Disk", value=panel.fmt_bytes(r["disk"]) + (f" / {d['disk']} MB" if d["disk"] else ""))
    e.add_field(name="Network", value=f"↓ {panel.fmt_bytes(r['rx'])} · ↑ {panel.fmt_bytes(r['tx'])}")
    e.add_field(name="Uptime", value=panel.fmt_uptime(r["uptime"]) if r["state"] == "running" else "—")
    if d["address"]:
        e.add_field(name="Address", value=f"`{d['address']}`")
    e.set_footer(text=f"{ident} · updated {time.strftime('%H:%M:%S')} UTC")
    return ident, e

class PowerView(discord.ui.View):
    def __init__(self, gid, ident):
        super().__init__(timeout=900)
        self.gid, self.ident = gid, ident

    async def _act(self, inter, signal):
        if not can_panel(inter.user, self.gid):
            return await _deny(inter, "You don't have access to the game panel here.")
        if not store.quota(self.gid, inter.user.id, "panel"):
            from core import limit_text
            return await _deny(inter, limit_text("panel"))
        await inter.response.defer()
        try:
            if signal != "refresh":
                await panel.power(self.gid, self.ident, signal)
                await audit(self.gid, inter.user, f"power {signal} (button)", self.ident)
                await asyncio.sleep(2)
            _, e = await status_embed(self.gid, self.ident)
            if signal != "refresh":
                e.description += f"\n-# {signal} by {inter.user.display_name}"
            await inter.edit_original_response(embed=e, view=self)
        except panel.PanelError as ex:
            await inter.followup.send(f"⚠️ {ex}", ephemeral=True)

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success)
    async def b_start(self, inter, _):
        await self._act(inter, "start")

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.primary)
    async def b_restart(self, inter, _):
        await self._act(inter, "restart")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def b_stop(self, inter, _):
        await self._act(inter, "stop")

    @discord.ui.button(label="Kill", emoji="💀", style=discord.ButtonStyle.secondary)
    async def b_kill(self, inter, _):
        await self._act(inter, "kill")

    @discord.ui.button(label="Refresh", emoji="📊", style=discord.ButtonStyle.secondary)
    async def b_refresh(self, inter, _):
        await self._act(inter, "refresh")

@panel_grp.command(name="servers", description="List game servers on the panel")
async def panel_servers(inter: discord.Interaction):
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True)
    try:
        servers = await panel.list_servers(inter.guild_id, fresh=True)
        lines = [f"• **{n}** `{i}`" for i, n in servers[:40]] or ["No servers on this key."]
        await inter.followup.send(embed=embed(f"🎮 Game servers ({len(servers)})", "\n".join(lines)))
    except panel.PanelError as e:
        await inter.followup.send(f"⚠️ {e}")

@panel_grp.command(name="status", description="Live status of a game server, with power buttons")
@app_commands.autocomplete(server=server_autocomplete)
async def panel_status(inter: discord.Interaction, server: str):
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True)
    try:
        ident, e = await status_embed(inter.guild_id, server)
        await inter.followup.send(embed=e, view=PowerView(inter.guild_id, ident))
    except panel.PanelError as ex:
        await inter.followup.send(f"⚠️ {ex}")

@panel_grp.command(name="power", description="Start, stop, restart or kill a game server")
@app_commands.autocomplete(server=server_autocomplete)
async def panel_power_cmd(inter: discord.Interaction, server: str,
                          action: Literal["start", "stop", "restart", "kill"]):
    if not await _gate(inter):
        return
    await inter.response.defer(thinking=True)
    try:
        ident, name = await panel.resolve(inter.guild_id, server)
        await panel.power(inter.guild_id, ident, action)
        await audit(inter.guild_id, inter.user, f"power {action}", name)
        await inter.followup.send(f"✅ Sent **{action}** to **{name}**.")
    except panel.PanelError as ex:
        await inter.followup.send(f"⚠️ {ex}")

@panel_grp.command(name="console", description="Send a console command (panel admins)")
@app_commands.autocomplete(server=server_autocomplete)
async def panel_console(inter: discord.Interaction, server: str, command: app_commands.Range[str, 1, 500]):
    if not await _gate(inter, admin=True):
        return
    await inter.response.defer(thinking=True, ephemeral=True)
    try:
        ident, name = await panel.resolve(inter.guild_id, server)
        await panel.command(inter.guild_id, ident, command)
        await audit(inter.guild_id, inter.user, f"console: {command[:100]}", name)
        await inter.followup.send(f"✅ Sent to **{name}**: `{command[:150]}`", ephemeral=True)
    except panel.PanelError as ex:
        await inter.followup.send(f"⚠️ {ex}", ephemeral=True)

@panel_grp.command(name="logs", description="Show the latest console output (panel admins)")
@app_commands.autocomplete(server=server_autocomplete)
async def panel_logs_cmd(inter: discord.Interaction, server: str):
    if not await _gate(inter, admin=True):
        return
    await inter.response.defer(thinking=True, ephemeral=True)
    try:
        ident, name = await panel.resolve(inter.guild_id, server)
        status, lines = await panel.console_logs(inter.guild_id, ident)
        body = "\n".join(lines)[-1800:] or "(empty)"
        await inter.followup.send(f"📜 **{name}** ({status})\n```\n{body}\n```", ephemeral=True)
    except panel.PanelError as ex:
        await inter.followup.send(f"⚠️ {ex}", ephemeral=True)

tree.add_command(panel_grp)
