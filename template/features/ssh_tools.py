"""SSH to a server admin's own VPS: /ssh commands, AI tools, approval buttons, audit log.

Who can do what (per Discord server):
  link / remove VPS, settings, approve changes  → server owner or Administrator (and the bot owner)
  read-only checks (health, logs, disk…)        → those, plus the optional "SSH role"
  auto mode (changes run without approval)      → only the Discord server owner can turn it on
"""
import time
from typing import Literal, Optional

import discord
from discord import app_commands

import store, sshops, workspace
from sshops import SSHError
from core import tree, embed, is_owner, safe_send, log
from agent import tool, Result

APPROVAL_TTL = 600

# ---------- settings & access ----------
def cfg_of(gid):
    g = store.guild(gid)
    g.setdefault("ssh", {"hosts": {}, "role": None, "log_channel": None, "mode": "approve"})
    g["ssh"].setdefault("client_help", False)
    return g["ssh"]

# ---------- client sessions: a member lets __BOT_NAME__ into THEIR OWN VPS for a while ----------
# Kept in memory only (never written to disk), one per member, ended by /vps end or after SESSION_TTL.
SESSION_TTL = 3600
SESSIONS = {}                                       # (gid, user_id) -> {"h": host dict, "secret": str, "until": ts}

def session(gid, user_id):
    s = SESSIONS.get((gid, user_id))
    if s and time.time() > s["until"]:
        SESSIONS.pop((gid, user_id), None)
        return None
    return s

def client_help_on(gid):
    return bool(gid and cfg_of(gid).get("client_help"))

def is_ssh_admin(member, gid):
    if is_owner(member):
        return True
    if not isinstance(member, discord.Member):
        return False
    return member.guild.owner_id == member.id or member.guild_permissions.administrator

def can_use(member, gid):
    if is_ssh_admin(member, gid):
        return True
    role = cfg_of(gid)["role"]
    return bool(role and isinstance(member, discord.Member) and any(r.id == role for r in member.roles))

def _linked(c):
    return bool(c.gid and (cfg_of(c.gid)["hosts"] or client_help_on(c.gid)))

def _limit(gid):
    tier = store.guild(gid).get("tier", "free")
    return store.cfg["limits"].get(tier, {}).get("ssh", 40 if tier == "free" else 400)

def _quota(gid, user_id):
    return user_id == store.OWNER_ID or store.take(f"g:{gid}", "ssh", _limit(gid))

def _host(gid, name, user=None):
    name = (name or "").strip().lower()
    sess = session(gid, getattr(user, "id", None)) if user is not None else None
    if sess and (not name or name in ("client", "my-vps", "my vps", "vps", sess["h"]["name"])):
        return sess["h"]["name"], sess["h"]
    hosts = cfg_of(gid)["hosts"]
    if user is not None and not can_use(user, gid):
        if client_help_on(gid):
            raise SSHError("no shared VPS session from this user yet — call vps_connect to show them the safe connect buttons (or they use `/vps connect`)")
        raise SSHError("you don't have SSH access here")
    if not name and len(hosts) == 1:
        name = next(iter(hosts))
    if name not in hosts:
        raise SSHError(f"no VPS named `{name or '?'}`. Linked: " + (", ".join(f"`{h}`" for h in hosts) or "none"))
    return name, hosts[name]

async def audit(gid, user, host, cmd, level, status, secs=0.0):
    who = f"{getattr(user, 'display_name', user)} ({getattr(user, 'id', '?')})"
    line = f"[ssh] {who} on {host}: {cmd[:300]!r} → {status}"
    workspace.note(f"guild {gid}: {line}")
    log.info("guild %s %s", gid, line)
    ch_id = cfg_of(gid)["log_channel"]
    if ch_id:
        from core import client
        ch = client.get_channel(ch_id)
        if ch:
            icon = {"read": "🔍", "change": "🛠️", "blocked": "⛔"}.get(level, "🖥️")
            await safe_send(ch, embed=embed(desc=f"{icon} **{host}** · {status}" + (f" · {secs:.1f}s" if secs else "")
                                            + f"\n```sh\n{cmd[:900]}\n```-# by {getattr(user, 'mention', who)}"))

async def execute(gid, user, host_name, h, cmd, level):
    """Actually run (already approved / allowed). Returns (exit, output, secs)."""
    try:
        if h.get("auth") == "tmate":
            code, out, secs = await sshops.run_tmate(gid, h, cmd)
        else:
            secret = h["_plain"] if "_plain" in h else store.decrypt(h["secret"])
            code, out, secs = await sshops.run(gid, h, secret, cmd, owner=is_owner(user))
    except SSHError as e:
        await audit(gid, user, host_name, cmd, level, f"error: {e}")
        raise
    await audit(gid, user, host_name, cmd, level, f"exit {code}", secs)
    return code, out, secs

def _block(text, lang="sh"):
    return f"```{lang}\n{text[:1800]}\n```"

# ---------- approval buttons ----------
class ApproveView(discord.ui.View):
    def __init__(self, gid, requester_id, host_name, cmd, why, client_id=None):
        super().__init__(timeout=APPROVAL_TTL)
        self.gid, self.requester_id, self.host_name, self.cmd, self.why = gid, requester_id, host_name, cmd, why
        self.client_id = client_id                  # their own VPS: only they approve
        self.done = False
        self.message = None

    def text(self):
        who = f"<@{self.client_id}> (it's your VPS)" if self.client_id else "Server owner / Administrators"
        return (f"🛠️ **SSH change waiting for approval** on `{self.host_name}`\n{_block(self.cmd)}"
                f"Why it needs approval: {self.why}\n-# {who} can approve. Expires in {APPROVAL_TTL // 60} min.")

    async def interaction_check(self, inter):
        if self.client_id:
            if inter.user.id != self.client_id:
                await inter.response.send_message("Only the owner of this VPS can approve.", ephemeral=True)
                return False
            return True
        if not is_ssh_admin(inter.user, self.gid):
            await inter.response.send_message("Only the server owner or an Administrator can approve SSH changes.",
                                              ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if not self.done and self.message:
            try:
                await self.message.edit(content=self.text() + "\n⌛ Expired — nothing was run.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Approve & run", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, inter, _):
        if self.done:
            return await inter.response.send_message("Already handled.", ephemeral=True)
        self.done = True
        self.stop()
        await inter.response.edit_message(content=self.text() + f"\n⏳ Approved by {inter.user.mention}, running…",
                                          view=None)
        try:
            if self.client_id:
                sess = session(self.gid, self.client_id)
                if not sess:
                    raise SSHError("the VPS session has ended")
                h = sess["h"]
            else:
                _, h = _host(self.gid, self.host_name)
            if sshops.classify(self.cmd)[0] == "blocked":        # re-check: rules may have changed
                raise SSHError("this command is blocked")
            code, out, secs = await execute(self.gid, inter.user, self.host_name, h, self.cmd, "change")
            res = f"{'✅' if code == 0 else '⚠️'} exit {code} · {secs:.1f}s\n{_block(out, '')}"
        except SSHError as e:
            res = f"⚠️ {e}"
        await inter.edit_original_response(
            content=(f"🛠️ **SSH change** on `{self.host_name}` · approved by {inter.user.mention}\n"
                     f"{_block(self.cmd)}{res}")[:1990])

    @discord.ui.button(label="Deny", emoji="✖️", style=discord.ButtonStyle.danger)
    async def deny(self, inter, _):
        self.done = True
        self.stop()
        await audit(self.gid, inter.user, self.host_name, self.cmd, "change", "denied")
        await inter.response.edit_message(content=self.text() + f"\n✖️ Denied by {inter.user.mention}.", view=None)

async def request(gid, user, host_name, cmd, *, channel=None, queue=None):
    """Central gate for every command. Returns (kind, payload):
       ('ran', (code, out, secs)) · ('pending', view) · ('refused', reason)."""
    s = cfg_of(gid)
    host_name, h = _host(gid, host_name, user)
    client = bool(h.get("client"))
    level, why = sshops.classify(cmd)
    if level == "blocked":
        await audit(gid, user, host_name, cmd, level, f"BLOCKED ({why})")
        return "refused", f"⛔ Blocked for safety: {why}. __BOT_NAME__ never runs this kind of command."
    if not _quota(gid, getattr(user, "id", 0)):
        return "refused", "📉 Today's SSH limit for this server is used up. It resets at 00:00 UTC."
    if level == "change":
        if s["mode"] == "readonly" and not client:
            return "refused", f"🔒 This server is in read-only SSH mode, so changes are off ({why})."
        if client or s["mode"] != "auto" or not is_ssh_admin(user, gid):
            view = ApproveView(gid, getattr(user, "id", 0), host_name, cmd, why,
                               client_id=getattr(user, "id", 0) if client else None)
            if queue is not None:
                queue.append((view.text(), view, {}))
            elif channel is not None:
                view.message = await channel.send(view.text(), view=view)
            return "pending", view
    return "ran", await execute(gid, user, host_name, h, cmd, level)

# =====================================================================
# AI tools
# =====================================================================
def _use_ok(c):
    return _linked(c) and (can_use(c.user, c.gid) or bool(session(c.gid, c.user.id)))

def _wrap(fn):
    async def inner(c, a):
        try:
            return await fn(c, a)
        except SSHError as e:
            return Result(f"SSH: {e}")
    return inner

def _result(host, cmd, kind, payload):
    if kind == "refused":
        return Result(payload, "⛔ SSH refused")
    if kind == "pending":
        return Result(f"This command changes the VPS, so an approval box with buttons is shown below your reply. "
                      f"Tell the user an admin must press Approve; it runs then and shows the output. "
                      f"Command: {cmd}", "🛠️ waiting for approval")
    code, out, secs = payload
    return Result(f"[{host}] $ {cmd}\nexit {code} ({secs:.1f}s)\n{out}", f"🖥️ ran on {host}")

@tool("ssh_hosts", "ssh_hosts", "list the VPS servers linked over SSH and the ready-made checks/fixes", when=_use_ok,
      progress="🖥️ Listing VPS…")
async def t_hosts(c, a):
    s = cfg_of(c.gid)
    hosts = "; ".join(f"{n} ({h['user']}@{h['host']}:{h['port']})" for n, h in s["hosts"].items()
                      if can_use(c.user, c.gid))
    sess = session(c.gid, c.user.id)
    if sess:
        hosts = (hosts + "; " if hosts else "") + f"{sess['h']['name']} (the user's own VPS, shared via {sess['h']['auth']})"
    return Result(f"VPS: {hosts}. Mode: {s['mode']}. Templates:\n{sshops.template_list()}", "🖥️ listed VPS")

@tool("ssh_check", "ssh_check | <vps> | <template> | <service, if needed>",
      "run a ready-made check or fix on a VPS (health, disk, memory, errors, logs, status, ports, docker, wings, "
      "restart, free_disk, renew_ssl…). Prefer this over ssh_run", when=_use_ok, progress="🖥️ Checking the VPS…",
      heavy=True)
@_wrap
async def t_check(c, a):
    a = (a + ["", "", ""])[:3]
    if not a[1] and a[0].strip().lower() in sshops.TEMPLATES:      # "ssh_check | health" with one VPS
        a = ["", a[0], a[1]]
    name, _ = _host(c.gid, a[0], c.user)
    cmd = sshops.template_cmd(a[1], a[2])
    kind, payload = await request(c.gid, c.user, name, cmd, queue=c.after_send)
    return _result(name, cmd, kind, payload)

@tool("ssh_run", "ssh_run | <vps> | <shell command>",
      "run one shell command on a VPS. Read-only commands run at once; changes need an admin's approval; "
      "dangerous ones are blocked", when=_use_ok, progress="🖥️ Running on the VPS…", heavy=True)
@_wrap
async def t_run(c, a):
    if len(a) < 2:
        if len(cfg_of(c.gid)["hosts"]) != 1 and not session(c.gid, c.user.id):
            return Result("Usage: ssh_run | <vps> | <command>")
        a = [""] + a
    name, _ = _host(c.gid, a[0], c.user)
    cmd = " | ".join(a[1:]).strip()        # the command itself may contain pipes
    kind, payload = await request(c.gid, c.user, name, cmd, queue=c.after_send)
    return _result(name, cmd, kind, payload)

# =====================================================================
# client help: a member shares their own VPS (tmate or IP + password)
# =====================================================================
TMATE_GUIDE = (
    "**Option 1 — tmate (recommended, no password shared)**\n"
    "Run on your VPS:\n"
    "```sh\nsudo apt install -y tmate    # Debian/Ubuntu  (CentOS/Alma: sudo dnf install -y tmate)\ntmate\n```"
    "Then run `tmate show-messages` (or look at the bottom bar) and copy the line that looks like\n"
    "`ssh AbC123xyz@sgp1.tmate.io` — **not** the one starting with `ro-`. Press **Connect with tmate** and paste it.\n"
    "To stop my access any time: type `exit` in tmate, or `/vps end`.\n\n"
    "**Option 2 — IP + password**: press **Connect with IP & password** (private form, never posted in chat). "
    "Change the password after we're done.\n"
    "-# ⚠️ Never paste a password in the chat. Access ends automatically after 1 hour. "
    "Every command is shown, and anything that changes your VPS waits for **your** Approve click.")

async def _start_session(inter, h, secret=None):
    gid, uid = inter.guild_id, inter.user.id
    if not (is_owner(inter.user) or store.take(f"u:{uid}", "vpsconnect", 6)):
        return await inter.followup.send("📉 Too many connect attempts today. Try again tomorrow.", ephemeral=True)
    h["name"] = "client-" + "".join(ch for ch in inter.user.name.lower() if ch.isalnum())[:16]
    h["client"] = True
    try:
        if h["auth"] == "tmate":
            code, out, _ = await sshops.run_tmate(gid, h, "whoami; uname -sr", timeout=20)
        else:
            conn, fp = await sshops.connect(h, secret)
            try:
                r = await conn.run("whoami; uname -sr", check=False)
            finally:
                conn.close()
            out = (r.stdout or "").strip()
            h["fingerprint"], h["_plain"] = fp, secret
    except SSHError as e:
        return await inter.followup.send(f"⚠️ Couldn't connect: {e}", ephemeral=True)
    SESSIONS[(gid, uid)] = {"h": h, "until": time.time() + SESSION_TTL}
    await audit(gid, inter.user, h["name"], f"client session started ({h['auth']}) {h['user']}@{h['host']}", "read",
                "connected")
    lines = out.splitlines()
    await inter.followup.send(f"✅ Connected to your VPS (`{lines[0] if lines else '?'}` · "
                              f"{lines[1] if len(lines) > 1 else 'Linux'}). "
                              f"Access ends in 1 hour or with `/vps end`.", ephemeral=True)
    if inter.channel:
        await safe_send(inter.channel, f"🔗 {inter.user.mention} shared their VPS with me for 1 hour. Tell me what's "
                                       f"wrong — I'll check first, and ask you before changing anything.")

class TmateModal(discord.ui.Modal, title="Connect with tmate"):
    line = discord.ui.TextInput(label="Paste the tmate ssh line", placeholder="ssh AbC123xyz@sgp1.tmate.io",
                                max_length=200)

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        try:
            h = sshops.parse_tmate(str(self.line.value))
        except SSHError as e:
            return await inter.followup.send(f"⚠️ {e}", ephemeral=True)
        if not h:
            return await inter.followup.send("⚠️ That isn't a tmate line. It looks like `ssh AbC123xyz@sgp1.tmate.io`.",
                                             ephemeral=True)
        await _start_session(inter, h)

class PasswordModal(discord.ui.Modal, title="Connect with IP & password"):
    host = discord.ui.TextInput(label="VPS IP (and port if not 22)", placeholder="203.0.113.10:22", max_length=120)
    user = discord.ui.TextInput(label="Username", placeholder="root", max_length=40)
    password = discord.ui.TextInput(label="Password (used for this session only)", max_length=200)

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        hp = str(self.host.value).strip()
        host, _, port = hp.rpartition(":") if hp.count(":") == 1 else (hp, "", "22")
        h = {"host": host or hp, "port": int(port) if str(port).isdigit() else 22, "user": str(self.user.value).strip(),
             "auth": "password"}
        await _start_session(inter, h, str(self.password.value))

class ConnectView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=1800)
        self.user_id = user_id
        self.message = None

    async def interaction_check(self, inter):
        if inter.user.id != self.user_id:
            await inter.response.send_message("This is for the person who asked. Ask me yourself to get your own.",
                                              ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Connect with tmate", emoji="🔗", style=discord.ButtonStyle.success)
    async def b_tmate(self, inter, _):
        await inter.response.send_modal(TmateModal())

    @discord.ui.button(label="Connect with IP & password", emoji="🔑", style=discord.ButtonStyle.secondary)
    async def b_pass(self, inter, _):
        await inter.response.send_modal(PasswordModal())

@tool("vps_connect", "vps_connect", "a member needs help with THEIR OWN VPS: show the safe way to give you access "
      "(tmate guide + private connect buttons). Never ask for passwords in chat",
      when=lambda c: client_help_on(c.gid) and not session(c.gid, getattr(c.user, "id", 0)),
      progress="🔗 Preparing a secure connect…")
async def t_connect(c, a):
    view = ConnectView(c.user.id)
    c.after_send.append((f"🖥️ **Let me into your VPS** <@{c.user.id}>\n\n" + TMATE_GUIDE, view, {}))
    return Result("A guide with private Connect buttons is shown below your reply. Tell the user to follow it; once "
                  "connected, you can run checks with ssh_check / ssh_run (their VPS is used automatically). "
                  "Don't repeat the guide.", "🔗 sent connect guide")

async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

vps_grp = app_commands.Group(name="vps", description="Let __BOT_NAME__ into your own VPS for a while to fix something",
                             guild_only=True)

@vps_grp.command(name="connect", description="Share your VPS with __BOT_NAME__ (tmate or IP + password, 1 hour)")
async def vps_connect(inter: discord.Interaction):
    if not client_help_on(inter.guild_id):
        return await _deny(inter, "VPS help isn't turned on in this server (admins: `/ssh settings client_help:True`).")
    await inter.response.send_message(TMATE_GUIDE, view=ConnectView(inter.user.id), ephemeral=True)

@vps_grp.command(name="end", description="Stop __BOT_NAME__'s access to your VPS now")
async def vps_end(inter: discord.Interaction):
    sess = SESSIONS.pop((inter.guild_id, inter.user.id), None)
    if not sess:
        return await _deny(inter, "You have no VPS session open.")
    await audit(inter.guild_id, inter.user, sess["h"]["name"], "client session ended", "read", "ended")
    await inter.response.send_message("🔒 Done — I no longer have access to your VPS. If you used a password, "
                                      "consider changing it. With tmate, type `exit` to close it too.", ephemeral=True)

@vps_grp.command(name="status", description="Is __BOT_NAME__ connected to your VPS?")
async def vps_status(inter: discord.Interaction):
    sess = session(inter.guild_id, inter.user.id)
    if not sess:
        return await _deny(inter, "Not connected. Use `/vps connect`.")
    h = sess["h"]
    await inter.response.send_message(f"🔗 Connected to `{h['user']}@{h['host']}` ({h['auth']}) · ends "
                                      f"<t:{int(sess['until'])}:R> · `/vps end` to stop now.", ephemeral=True)

tree.add_command(vps_grp)

# =====================================================================
# /ssh commands
# =====================================================================
ssh_grp = app_commands.Group(name="ssh", description="Let __BOT_NAME__ check and fix your VPS over SSH (with safety limits)",
                             guild_only=True, default_permissions=discord.Permissions(administrator=True))

async def _deny(inter, text):
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)

async def host_ac(inter: discord.Interaction, current: str):
    if not can_use(inter.user, inter.guild_id):
        return []
    return [app_commands.Choice(name=n, value=n) for n in cfg_of(inter.guild_id)["hosts"] if current.lower() in n][:25]

async def template_ac(inter: discord.Interaction, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=f"{k} — {d}"[:100], value=k) for k, (d, _) in sshops.TEMPLATES.items()
            if cur in k or cur in d.lower()][:25]

class AddModal(discord.ui.Modal, title="Link a VPS over SSH"):
    name = discord.ui.TextInput(label="Short name", placeholder="main-vps", max_length=24)
    host = discord.ui.TextInput(label="Host / IP and port", placeholder="203.0.113.10:22", max_length=120)
    user = discord.ui.TextInput(label="Username", placeholder="__bot_slug__ (a dedicated user is safest)", max_length=40)
    secret = discord.ui.TextInput(label="Password OR private key (unencrypted)", style=discord.TextStyle.paragraph,
                                  placeholder="Paste a password, or the whole -----BEGIN … PRIVATE KEY----- block",
                                  max_length=4000)

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        gid = inter.guild_id
        name = "".join(ch for ch in str(self.name.value).lower().strip().replace(" ", "-") if ch.isalnum() or ch in "-_")
        hostport = str(self.host.value).strip()
        host, _, port = hostport.rpartition(":") if hostport.count(":") == 1 else (hostport, "", "22")
        secret = str(self.secret.value).strip()
        h = {"host": host or hostport, "port": int(port or 22) if str(port or 22).isdigit() else 22,
             "user": str(self.user.value).strip(), "auth": "key" if "PRIVATE KEY" in secret else "password"}
        if not name:
            return await inter.followup.send("⚠️ Give it a short name like `main-vps`.", ephemeral=True)
        if len(cfg_of(gid)["hosts"]) >= 5 and name not in cfg_of(gid)["hosts"]:
            return await inter.followup.send("⚠️ Up to 5 VPS per Discord server.", ephemeral=True)
        try:
            conn, fp = await sshops.connect(h, secret, owner=is_owner(inter.user))
            try:
                r = await conn.run("whoami; uname -sr; (sudo -n true 2>/dev/null && echo SUDO_OK) || echo NO_SUDO",
                                   check=False)
            finally:
                conn.close()
        except SSHError as e:
            return await inter.followup.send(f"⚠️ Couldn't link: {e}", ephemeral=True)
        info = (r.stdout or "").strip().splitlines()
        h["fingerprint"], h["secret"], h["added_by"], h["added"] = fp, store.encrypt(secret), inter.user.id, time.time()
        cfg_of(gid)["hosts"][name] = h
        store.save_guild(gid)
        await audit(gid, inter.user, name, f"linked {h['user']}@{h['host']}:{h['port']}", "change", "linked")
        root = info and info[0] == "root"
        sudo = "SUDO_OK" in info
        await inter.followup.send(
            f"✅ Linked **{name}** — `{h['user']}@{h['host']}:{h['port']}` ({info[1] if len(info) > 1 else 'Linux'})\n"
            f"Login is stored **encrypted**; the server's identity is pinned (`{fp[:24]}…`).\n"
            + ("⚠️ This is the **root** account. Safer: a dedicated user with limited sudo.\n" if root else
               "" if sudo else "ℹ️ This user has no password-less sudo, so fixes that need root will fail.\n")
            + f"Mode: **{cfg_of(gid)['mode']}** — checks run at once, changes need an admin's approval.\n"
              "Try: `/ssh check` → `health`, or ask me *\"why is my VPS slow?\"*", ephemeral=True)

@ssh_grp.command(name="add", description="Link a VPS (password or private key, stored encrypted)")
async def ssh_add(inter: discord.Interaction):
    if not is_ssh_admin(inter.user, inter.guild_id):
        return await _deny(inter, "Only the server owner or an Administrator can link a VPS.")
    await inter.response.send_modal(AddModal())

@ssh_grp.command(name="remove", description="Unlink a VPS and delete its saved login")
@app_commands.autocomplete(vps=host_ac)
async def ssh_remove(inter: discord.Interaction, vps: str):
    if not is_ssh_admin(inter.user, inter.guild_id):
        return await _deny(inter, "Only the server owner or an Administrator can do that.")
    hosts = cfg_of(inter.guild_id)["hosts"]
    if hosts.pop(vps.lower(), None) is None:
        return await _deny(inter, f"No VPS named `{vps}`.")
    store.save_guild(inter.guild_id)
    await audit(inter.guild_id, inter.user, vps, "unlinked", "change", "removed")
    await inter.response.send_message(f"🗑️ Unlinked `{vps}` and deleted its saved login.", ephemeral=True)

@ssh_grp.command(name="list", description="Linked VPS, mode and today's usage")
async def ssh_list(inter: discord.Interaction):
    gid = inter.guild_id
    if not can_use(inter.user, gid):
        return await _deny(inter, "You don't have SSH access here.")
    s = cfg_of(gid)
    lines = [f"• **{n}** `{h['user']}@{h['host']}:{h['port']}` · {h['auth']}" for n, h in s["hosts"].items()]
    e = embed("🖥️ Linked VPS", "\n".join(lines) or "None yet — `/ssh add`.")
    e.add_field(name="Mode", value={"readonly": "🔒 read-only", "approve": "✅ changes need approval",
                                    "auto": "⚡ auto (admins' changes run at once)"}[s["mode"]])
    e.add_field(name="SSH role", value=f"<@&{s['role']}>" if s["role"] else "admins only")
    e.add_field(name="Log channel", value=f"<#{s['log_channel']}>" if s["log_channel"] else "none")
    e.add_field(name="Member VPS help", value="on" if s.get("client_help") else "off")
    e.add_field(name="Today", value=f"{store.used(f'g:{gid}', 'ssh')}/{_limit(gid)} commands")
    await inter.response.send_message(embed=e, ephemeral=True)

@ssh_grp.command(name="settings", description="Safety mode, who can run checks, where actions are logged")
@app_commands.describe(mode="readonly = checks only · approve = changes need an admin click · auto = admins' changes "
                       "run at once (server owner only)", role="Members who may run read-only checks",
                       log_channel="Every SSH command is logged here (recommended)",
                       client_help="Members may share their OWN VPS with __BOT_NAME__ (tmate or IP+password, 1 hour)")
async def ssh_settings(inter: discord.Interaction, mode: Optional[Literal["readonly", "approve", "auto"]] = None,
                       role: Optional[discord.Role] = None, log_channel: Optional[discord.TextChannel] = None,
                       clear_role: bool = False, client_help: Optional[bool] = None):
    gid = inter.guild_id
    if not is_ssh_admin(inter.user, gid):
        return await _deny(inter, "Only the server owner or an Administrator can change SSH settings.")
    s = cfg_of(gid)
    if mode == "auto" and not (inter.guild.owner_id == inter.user.id or is_owner(inter.user)):
        return await _deny(inter, "Only the Discord **server owner** can turn on auto mode.")
    if mode:
        s["mode"] = mode
    if role:
        s["role"] = role.id
    if clear_role:
        s["role"] = None
    if log_channel:
        s["log_channel"] = log_channel.id
    if client_help is not None:
        s["client_help"] = client_help
    store.save_guild(gid)
    await audit(gid, inter.user, "-", f"settings mode={s['mode']} role={s['role']} log={s['log_channel']}",
                "change", "updated")
    role_txt = "<@&%s>" % s["role"] if s["role"] else "admins only"
    log_txt = "<#%s>" % s["log_channel"] if s["log_channel"] else "none"
    help_txt = "on" if s.get("client_help") else "off"
    await inter.response.send_message(f"✅ SSH mode **{s['mode']}** · role {role_txt} · log {log_txt} · "
                                      f"member VPS help **{help_txt}**", ephemeral=True)

async def _slash_run(inter, vps, cmd):
    gid = inter.guild_id
    if not (can_use(inter.user, gid) or session(gid, inter.user.id)):
        return await _deny(inter, "You don't have SSH access here.")
    await inter.response.defer(thinking=True)
    try:
        kind, payload = await request(gid, inter.user, vps, cmd, channel=inter.channel)
    except SSHError as e:
        return await inter.followup.send(f"⚠️ {e}")
    if kind == "refused":
        return await inter.followup.send(payload)
    if kind == "pending":
        return await inter.followup.send("🛠️ That changes the VPS — approval box posted above.")
    code, out, secs = payload
    await inter.followup.send((f"🖥️ `{vps}` $ `{cmd[:150]}` · exit {code} · {secs:.1f}s\n{_block(out, '')}")[:1990])

@ssh_grp.command(name="check", description="Run a ready-made check or fix (health, disk, logs, restart…)")
@app_commands.autocomplete(vps=host_ac, template=template_ac)
@app_commands.describe(target="Service or container name, for logs/status/restart")
async def ssh_check(inter: discord.Interaction, vps: str, template: str, target: Optional[str] = None):
    try:
        cmd = sshops.template_cmd(template, target or "")
    except SSHError as e:
        return await _deny(inter, f"⚠️ {e}")
    await _slash_run(inter, vps, cmd)

@ssh_grp.command(name="run", description="Run a shell command (checks run at once, changes need approval)")
@app_commands.autocomplete(vps=host_ac)
async def ssh_run(inter: discord.Interaction, vps: str, command: str):
    await _slash_run(inter, vps, command)

@ssh_grp.command(name="templates", description="Show the ready-made checks and fixes")
async def ssh_templates(inter: discord.Interaction):
    await inter.response.send_message(embed=embed("🧰 SSH templates", sshops.template_list()[:4000]), ephemeral=True)

tree.add_command(ssh_grp)
