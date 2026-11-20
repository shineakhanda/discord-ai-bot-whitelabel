"""Hosting-owner panel moderation (Application API): users, all servers, nodes, abuse scan + background watch."""
import re, time, secrets, asyncio
from collections import defaultdict

import discord

import store, panel, workspace
from store import log
from core import client, embed, feature, can_panel_owner, safe_send, on_startup
from agent import tool, Result, split_args
from features.panel_tools import audit, _wrap

def owner_ok(c):
    return bool(c.gid and feature(c.gid, "panel") and panel.has_app(c.gid) and can_panel_owner(c.user, c.gid))

def _q(c):
    if not store.quota(c.gid, c.user.id, "panel"):
        raise panel.PanelError("Today's panel limit for this server is used up.")

def _u(u):
    servers = panel.user_servers(u)
    return (f"#{u.get('id')} {u.get('username')} <{u.get('email')}>{' [ROOT ADMIN]' if u.get('root_admin') else ''}"
            f"{' 2FA' if u.get('2fa') else ''} · {len(servers)} server(s)")

async def _dm_secret(c, title, text):
    try:
        await c.user.send(f"🔐 **{title}**\n{text}\n-# Sent privately by __BOT_NAME__. Delete this message after saving it.")
        return True
    except discord.HTTPException:
        return False

# ---------- users ----------
@tool("panel_users", "panel_users | <search: name or email, optional>", "list/search panel users", when=owner_ok,
      progress="👥 Looking up panel users…", heavy=True)
@_wrap
async def t_users(c, a):
    _q(c)
    us = await panel.users(c.gid, a[0] if a else "")
    return Result(f"{len(us)} user(s): " + "; ".join(
        f"#{u.get('id')} {u.get('username')} <{u.get('email')}>{' ROOT' if u.get('root_admin') else ''}" for u in us[:60]),
        "👥 listed users")

@tool("panel_user_info", "panel_user_info | <user id, username or email>", "a panel user and their servers",
      when=owner_ok, progress="👤 Checking panel user…", heavy=True)
@_wrap
async def t_user_info(c, a):
    _q(c)
    u = await panel.find_user(c.gid, a[0])
    servers = panel.user_servers(u)
    return Result(_u(u) + "\nServers: " + ("; ".join(f"{s.get('name')} [{s.get('identifier')}]"
                                                      f"{' SUSPENDED' if s.get('suspended') else ''}" for s in servers)
                                           or "none"), f"👤 {u.get('username')}")

@tool("panel_user_create", "panel_user_create | <email> | <username> | <first name last name, optional>",
      "create a panel account; the password is DMed to you", when=owner_ok, progress="👤 Creating panel user…",
      heavy=True)
@_wrap
async def t_user_create(c, a):
    _q(c)
    if len(a) < 2 or "@" not in a[0]:
        return Result("Format: panel_user_create | email | username | first last")
    first, _, last = (a[2] if len(a) > 2 else "").partition(" ")
    pw = secrets.token_urlsafe(12)
    u = await panel.create_user(c.gid, a[0], a[1], first, last, pw)
    sent = await _dm_secret(c, f"New panel account {u.get('username')}",
                            f"Panel: {store.guild(c.gid)['panel']['url']}\nUsername: `{u.get('username')}`\n"
                            f"Email: `{u.get('email')}`\nPassword: ||{pw}||")
    await audit(c.gid, c.user, f"created user {u.get('username')} (#{u.get('id')})")
    return Result(f"Created panel user #{u.get('id')} {u.get('username')}. Password "
                  f"{'sent to you by DM' if sent else 'could not be DMed (DMs closed) — reset it later'}. "
                  "Never post passwords in chat.", f"👤 created {u.get('username')}")

@tool("panel_user_password", "panel_user_password | <user>", "reset a user's password (new one is DMed to you)",
      when=owner_ok, progress="🔑 Resetting password…", heavy=True)
@_wrap
async def t_user_pw(c, a):
    _q(c)
    u = await panel.find_user(c.gid, a[0])
    pw = secrets.token_urlsafe(12)
    await panel.update_user(c.gid, u, password=pw)
    sent = await _dm_secret(c, f"New password for {u.get('username')}", f"Password: ||{pw}||")
    await audit(c.gid, c.user, f"reset password of {u.get('username')}")
    return Result(f"Password reset for {u.get('username')}; {'sent to you by DM' if sent else 'DM failed (DMs closed)'}.",
                  "🔑 password reset")

@tool("panel_user_admin", "panel_user_admin | <user> | on/off", "give or remove panel root admin", when=owner_ok,
      progress="🛡️ Updating admin rights…", heavy=True)
@_wrap
async def t_user_admin(c, a):
    _q(c)
    u = await panel.find_user(c.gid, a[0])
    on = (a[1].lower() if len(a) > 1 else "") in ("on", "yes", "true", "give", "add")
    await panel.update_user(c.gid, u, root_admin=on)
    await audit(c.gid, c.user, f"root admin {'ON' if on else 'OFF'} for {u.get('username')}")
    return Result(f"{u.get('username')} is {'now' if on else 'no longer'} a panel root admin.", "🛡️ admin rights")

@tool("panel_user_suspend", "panel_user_suspend | <user> | on/off", "suspend or unsuspend ALL servers of a user "
      "(e.g. unpaid, abuse)", when=owner_ok, progress="⛔ Updating user's servers…", heavy=True)
@_wrap
async def t_user_suspend(c, a):
    _q(c)
    u = await panel.find_user(c.gid, a[0])
    on = (a[1].lower() if len(a) > 1 else "on") in ("on", "yes", "true", "suspend")
    servers = panel.user_servers(u)
    done = 0
    for s in servers:
        try:
            await panel.app(c.gid, "POST", f"/servers/{s.get('id')}/{'suspend' if on else 'unsuspend'}")
            done += 1
        except panel.PanelError:
            pass
    await audit(c.gid, c.user, f"{'suspended' if on else 'unsuspended'} {done}/{len(servers)} servers of {u.get('username')}")
    return Result(f"{'Suspended' if on else 'Unsuspended'} {done}/{len(servers)} server(s) of {u.get('username')}.",
                  "⛔ user suspended" if on else "✅ user unsuspended")

@tool("panel_user_delete", "panel_user_delete | <user> | <'with servers' to delete their servers too>",
      "delete a panel user", when=owner_ok, progress="🗑️ Deleting panel user…", heavy=True)
@_wrap
async def t_user_delete(c, a):
    _q(c)
    u = await panel.find_user(c.gid, a[0])
    servers = panel.user_servers(u)
    if servers and not (len(a) > 1 and "server" in a[1].lower()):
        return Result(f"{u.get('username')} still owns {len(servers)} server(s). Delete those first, or confirm "
                      "'with servers'.")
    for s in servers:
        await panel.app(c.gid, "DELETE", f"/servers/{s.get('id')}")
    await panel.delete_user(c.gid, u["id"])
    panel.clear_cache(c.gid)
    await audit(c.gid, c.user, f"deleted user {u.get('username')} (#{u.get('id')}) and {len(servers)} server(s)")
    return Result(f"Deleted user {u.get('username')}" + (f" and {len(servers)} server(s)." if servers else "."),
                  f"🗑️ deleted {u.get('username')}")

# ---------- servers ----------
@tool("panel_server_find", "panel_server_find | <name, id, or owner username/email>", "find servers on the whole panel",
      when=owner_ok, progress="🔎 Searching the panel…", heavy=True)
@_wrap
async def t_server_find(c, a):
    _q(c)
    q = (a[0] if a else "").lower()
    servers = await panel.all_servers(c.gid)
    owners = {}
    if q:
        try:
            for u in await panel.users(c.gid, q):
                owners[u.get("id")] = u.get("username")
        except panel.PanelError:
            pass
    hits = [s for s in servers if not q or q in str(s.get("name", "")).lower() or q == str(s.get("identifier"))
            or s.get("user") in owners]
    return Result(f"{len(hits)} match(es): " + "; ".join(
        f"{s.get('name')} [{s.get('identifier')}] owner #{s.get('user')} node #{s.get('node')}"
        f"{' SUSPENDED' if s.get('suspended') else ''}" for s in hits[:50]), "🔎 searched panel")

@tool("panel_server_create", "panel_server_create | <name> | <owner user> | <egg> | <memory MB> | <disk MB> | <cpu %> | "
      "<location, optional>", "create a new server for a panel user", when=owner_ok,
      progress="🆕 Creating server…", heavy=True)
@_wrap
async def t_server_create(c, a):
    _q(c)
    if len(a) < 6:
        return Result("Format: panel_server_create | name | owner | egg | memory MB | disk MB | cpu % | location")
    nums = [int(re.sub(r"\D", "", x) or 0) for x in a[3:6]]
    owner = await panel.find_user(c.gid, a[1])
    srv, egg = await panel.server_create(c.gid, a[0], owner["id"], a[2], *nums, location=a[6] if len(a) > 6 else None)
    await audit(c.gid, c.user, f"created server {a[0]} [{srv.get('identifier')}] for {owner.get('username')} "
                f"({egg['name']}, {nums[0]}MB/{nums[1]}MB/{nums[2]}%)")
    return Result(f"Created {a[0]} [{srv.get('identifier')}] for {owner.get('username')} with {egg['name']} "
                  f"({nums[0]} MB RAM, {nums[1]} MB disk, {nums[2]}% CPU). It's installing now.",
                  f"🆕 created {a[0]}")

@tool("panel_server_delete", "panel_server_delete | <server> | <'force' optional>", "permanently delete a server",
      when=owner_ok, progress="🗑️ Deleting server…", heavy=True)
@_wrap
async def t_server_delete(c, a):
    _q(c)
    ident, name = await panel.resolve(c.gid, a[0])
    force = len(a) > 1 and "force" in a[1].lower()
    await panel.server_delete(c.gid, ident, force)
    await audit(c.gid, c.user, f"deleted server{' (force)' if force else ''}", name)
    return Result(f"Deleted {name}.", f"🗑️ deleted {name}")

@tool("panel_server_owner", "panel_server_owner | <server> | <new owner user>", "transfer a server to another user",
      when=owner_ok, progress="🔁 Transferring server…", heavy=True)
@_wrap
async def t_server_owner(c, a):
    _q(c)
    if len(a) < 2:
        return Result("Format: panel_server_owner | server | user")
    ident, name = await panel.resolve(c.gid, a[0])
    u = await panel.find_user(c.gid, a[1])
    await panel.server_rename(c.gid, ident, owner_id=u["id"])
    await audit(c.gid, c.user, f"owner → {u.get('username')}", name)
    return Result(f"{name} now belongs to {u.get('username')}.", "🔁 transferred")

@tool("panel_server_rename", "panel_server_rename | <server> | <new name>", "rename a server", when=owner_ok,
      progress="✏️ Renaming server…", heavy=True)
@_wrap
async def t_server_rename(c, a):
    _q(c)
    if len(a) < 2:
        return Result("Format: panel_server_rename | server | new name")
    ident, name = await panel.resolve(c.gid, a[0])
    await panel.server_rename(c.gid, ident, name=a[1])
    await audit(c.gid, c.user, f"renamed → {a[1]}", name)
    return Result(f"Renamed {name} → {a[1]}.", "✏️ renamed")

# ---------- nodes ----------
@tool("panel_nodes", "panel_nodes", "nodes with allocated RAM/disk vs capacity, maintenance mode, server count",
      when=owner_ok, progress="🖥️ Checking nodes…", heavy=True)
@_wrap
async def t_nodes(c, a):
    _q(c)
    ns = await panel.nodes(c.gid)
    servers = await panel.all_servers(c.gid)
    count = defaultdict(int)
    for s in servers:
        count[s.get("node")] += 1
    lines = []
    for n in ns:
        al = n.get("allocated_resources") or {}
        mem, disk = n.get("memory") or 0, n.get("disk") or 0
        lines.append(f"#{n.get('id')} {n.get('name')} ({n.get('fqdn')}): {count[n.get('id')]} servers, RAM "
                     f"{al.get('memory', '?')}/{mem} MB, disk {al.get('disk', '?')}/{disk} MB"
                     f"{' MAINTENANCE' if n.get('maintenance_mode') else ''}")
    return Result("\n".join(lines) or "No nodes.", "🖥️ checked nodes")

@tool("panel_node_maintenance", "panel_node_maintenance | <node> | on/off", "put a node in or out of maintenance",
      when=owner_ok, progress="🛠️ Updating node…", heavy=True)
@_wrap
async def t_node_maint(c, a):
    _q(c)
    on = (a[1].lower() if len(a) > 1 else "on") in ("on", "yes", "true")
    n = await panel.node_maintenance(c.gid, a[0], on)
    await audit(c.gid, c.user, f"node {n.get('name')} maintenance {'ON' if on else 'OFF'}")
    return Result(f"Node {n.get('name')} maintenance {'on' if on else 'off'}.", "🛠️ node updated")

@tool("panel_locations", "panel_locations", "list panel locations", when=owner_ok, progress="📍 Listing locations…",
      heavy=True)
@_wrap
async def t_locations(c, a):
    _q(c)
    ls = await panel.locations(c.gid)
    return Result("; ".join(f"#{l.get('id')} {l.get('short')} ({l.get('long') or ''})" for l in ls) or "none", "📍 locations")

# ---------- abuse scan ----------
def _scan_text(res, top=8):
    lines = [f"Scanned {res['scanned']}/{res['total']} servers ({res['suspended']} suspended)."]
    if res["flagged"]:
        lines.append("⚠️ Suspicious: " + "; ".join(f"{s.get('name')} [{s.get('identifier')}] owner #{s.get('user')}: {why}"
                                                  for s, why in res["flagged"][:15]))
    lines.append("Top CPU: " + "; ".join(f"{s.get('name')} [{s.get('identifier')}] {pct:.0f}% of limit, "
                                         f"RAM {panel.fmt_bytes(r['mem'])}" for s, r, pct in res["live"][:top]))
    ram = sorted(res["live"], key=lambda x: x[1]["mem"], reverse=True)[:5]
    lines.append("Top RAM: " + "; ".join(f"{s.get('name')} {panel.fmt_bytes(r['mem'])}" for s, r, _ in ram))
    return "\n".join(lines)

@tool("panel_scan", "panel_scan", "scan the whole panel for abuse: crypto miners, CPU/RAM hogs", when=owner_ok,
      progress="🕵️ Scanning all servers…", heavy=True)
@_wrap
async def t_scan(c, a):
    _q(c)
    res = await panel.scan(c.gid)
    return Result(_scan_text(res), f"🕵️ scanned {res['scanned']} servers")

# ---------- background watch (the panel never sleeps) ----------
_hot = defaultdict(lambda: defaultdict(int))     # gid -> identifier -> consecutive hot scans
_alerted = defaultdict(dict)                      # gid -> identifier -> ts

@on_startup
async def panel_watch():
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(900)
        for guild in list(client.guilds):
            p = store.guild(guild.id)["panel"]
            if not (p["watch"] and p["url"] and p["app_key"] and p["log_channel"]):
                continue
            try:
                res = await panel.scan(guild.id)
            except Exception as e:
                log.warning("panel watch %s: %s", guild.id, e)
                continue
            ch = guild.get_channel(p["log_channel"])
            now = time.time()
            for s, why in res["flagged"]:
                ident = s.get("identifier")
                if s.get("suspended") or now - _alerted[guild.id].get(ident, 0) < 6 * 3600:
                    continue
                _alerted[guild.id][ident] = now
                act = ""
                if p["watch_suspend"]:
                    try:
                        await panel.app(guild.id, "POST", f"/servers/{s.get('id')}/suspend")
                        act = " → **auto-suspended**"
                    except panel.PanelError:
                        act = " (auto-suspend failed)"
                workspace.note(f"guild {guild.id}: [panel watch] {s.get('name')} {why}{act}")
                if ch:
                    await safe_send(ch, embed=embed("🚨 Possible abuse on the panel",
                                                    f"**{s.get('name')}** `{ident}` (owner #{s.get('user')}): {why}{act}",
                                                    color=0xEF4444))
            hot_now = set()
            for s, r, pct in res["live"]:
                if pct >= p["watch_cpu"] and r["state"] == "running":
                    ident = s.get("identifier")
                    hot_now.add(ident)
                    _hot[guild.id][ident] += 1
                    if _hot[guild.id][ident] == 2 and now - _alerted[guild.id].get(ident, 0) > 6 * 3600:
                        _alerted[guild.id][ident] = now
                        if ch:
                            await safe_send(ch, embed=embed("🔥 Server maxing CPU for 30+ minutes",
                                                            f"**{s.get('name')}** `{ident}` at {pct:.0f}% of its CPU "
                                                            f"limit, RAM {panel.fmt_bytes(r['mem'])}. Ask me to check "
                                                            "it (`panel_logs`) or suspend it.", color=0xF59E0B))
            for ident in list(_hot[guild.id]):
                if ident not in hot_now:
                    del _hot[guild.id][ident]
