"""Owner-only prefix commands (global settings, limits, workspace, cleanup)."""
import re

import store, ai, web, panel, workspace
from store import cfg, OWNER_ID, PREFIX
from core import client, tree, chunks
from features import chat
import os, subprocess

def code_version():
    """Git commit the bot is running (set by the panel's git pull)."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h · %s · %cr"], cwd=store.BASE, capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:120]
    except Exception:
        pass
    return "unknown (not a git checkout)"

OWNER_HELP = (
    "**Owner commands**\n"
    f"`{PREFIX}model [id]` · `{PREFIX}fallback [add|del <id>|clear]` · `{PREFIX}tokens <n>` · `{PREFIX}temp <x>` · "
    f"`{PREFIX}turns <n>` · `{PREFIX}think off|low|medium|high` · `{PREFIX}hedge <sec>`\n"
    f"`{PREFIX}web off|manual|auto` · `{PREFIX}engine [google,serper,tavily,ddg]` · `{PREFIX}searchcap <n>`\n"
    f"`{PREFIX}limits` · `{PREFIX}limit <free|premium|dm> <chat|search|browse|panel|mod|announce> <n>` · `{PREFIX}usercap <n>`\n"
    f"`{PREFIX}premium <guild_id> on|off` · `{PREFIX}block|unblock <user>` · `{PREFIX}blockguild|unblockguild <id>`\n"
    f"`{PREFIX}memory [add <rule>|del <n>|clear]` standing instructions (workspace/MEMORY.md)\n"
    f"`{PREFIX}soul` · `{PREFIX}skills` · `{PREFIX}reload` workspace files (SOUL/AGENTS/TOOLS/skills)\n"
    f"`{PREFIX}version` running code version · `{PREFIX}backup` back up the database now\n"
    f"`{PREFIX}presence streaming [twitch url]` · `{PREFIX}statustext add|del|every` bot status\n"
    f"`{PREFIX}serverlog` every server + invite links (public or allowed) → CSV\n"
    f"`{PREFIX}smart <model|off>` stronger model for hard jobs · `{PREFIX}vision <model|off>` · `{PREFIX}audio <model|off>` · `{PREFIX}caps [model]` · `{PREFIX}history <n>`\n"
    f"`{PREFIX}pause` · `{PREFIX}resume` · `{PREFIX}resetall` · `{PREFIX}clearcache` · `{PREFIX}cleancommands`\n"
    f"`{PREFIX}status` · `{PREFIX}servers` · `{PREFIX}leave <id>` · `{PREFIX}sync`"
)
OWNER_CMDS = {"ohelp", "model", "fallback", "tokens", "temp", "turns", "think", "hedge", "web", "engine", "searchcap",
              "limits", "limit", "usercap", "premium", "block", "unblock", "blockguild", "unblockguild",
              "pause", "resume", "resetall", "status", "servers", "leave", "sync",
              "memory", "smart", "vision", "audio", "caps", "history", "clearcache", "cleancommands", "reload", "soul", "version", "backup", "presence", "statustext", "serverlog",
              "skills"}

def _id(arg):
    m = re.search(r"\d{15,21}", arg or "")
    return int(m.group()) if m else None

async def owner_cmd(msg, cmd, arg):
    send = msg.channel.send
    if cmd == "ohelp":
        return await send(OWNER_HELP)
    if cmd == "model":
        if arg:
            cfg["model"] = arg; store.save_cfg()
        return await send(f"Main model: `{cfg['model']}`")
    if cmd == "fallback":
        sub, _, val = arg.partition(" ")
        sub, val = sub.lower(), val.strip()
        if sub == "add" and val and val not in cfg["fallbacks"]:
            cfg["fallbacks"].append(val)
        elif sub in ("del", "remove") and val:
            cfg["fallbacks"] = [m for m in cfg["fallbacks"] if m != val]
        elif sub == "clear":
            cfg["fallbacks"] = []
        store.save_cfg()
        return await send("**Fallbacks:**\n" + ("\n".join(f"{i}. `{m}`" for i, m in enumerate(cfg["fallbacks"], 1)) or "none"))
    if cmd in ("tokens", "temp", "turns", "searchcap", "usercap"):
        if cmd == "tokens":
            cfg["max_tokens"] = max(16, min(4000, int(arg)))
        elif cmd == "temp":
            cfg["temperature"] = max(0.0, min(2.0, float(arg)))
        elif cmd == "turns":
            cfg["max_turns"] = max(1, min(30, int(arg))); ai.resize_ctx()
        elif cmd == "searchcap":
            cfg["search_cap"] = max(0, int(arg))
        else:
            cfg["user_daily_chat"] = max(1, int(arg))
        store.save_cfg()
        return await send(f"✅ {cmd} updated")
    if cmd == "hedge":
        try:
            cfg["hedge"] = max(0, min(120, float(arg)))
        except ValueError:
            return await send(f"Usage: `{PREFIX}hedge <seconds>` — if a model hasn't STARTED answering by then, "
                              f"ask the next backup. Answers already coming in are never cut. "
                              f"0 = always wait (now: {cfg.get('hedge', 20)}s)")
        store.save_cfg()
        return await send(f"✅ A model that stays silent {cfg['hedge']:g}s is skipped" if cfg["hedge"]
                          else f"✅ Always wait for `{cfg['model']}`")
    if cmd == "think":
        if arg.lower() not in ("off", "low", "medium", "high"):
            return await send(f"Usage: `{PREFIX}think off|low|medium|high` (now: {cfg['think']})")
        cfg["think"] = arg.lower(); store.save_cfg()
        return await send(f"✅ Thinking: {cfg['think']}")
    if cmd == "web":
        if arg.lower() not in ("off", "manual", "auto"):
            return await send(f"Usage: `{PREFIX}web off|manual|auto` (now: {cfg['web']})")
        cfg["web"] = arg.lower(); store.save_cfg()
        return await send(f"✅ Web search: {cfg['web']}")
    if cmd == "engine":
        valid = ("google", "serper", "tavily", "ddg")
        if arg:
            order = [e.strip().lower() for e in arg.replace(" ", ",").split(",") if e.strip()]
            bad = [e for e in order if e not in valid]
            if bad:
                return await send(f"Unknown engine: {', '.join(bad)}. Use: {', '.join(valid)}")
            cfg["engines"] = order; store.save_cfg()
        return await send("**Search engines (in order):**\n" + "\n".join(
            f"{i}. {e} {'✅' if web.engine_ok(e) else '❌'}" for i, e in enumerate(cfg["engines"], 1)))
    if cmd == "limits":
        lines = [f"**{tier}**: " + ", ".join(f"{k} {v}" for k, v in lim.items()) for tier, lim in cfg["limits"].items()]
        lines.append("**dm (per user)**: " + ", ".join(f"{k} {v}" for k, v in cfg["dm_limits"].items()))
        lines.append(f"**per user chat/day**: {cfg['user_daily_chat']} · **global searches/day**: {cfg['search_cap']}")
        return await send("\n".join(lines))
    if cmd == "limit":
        p = arg.split()
        if len(p) != 3 or p[1] not in ("chat", "search", "browse", "panel", "mod", "announce"):
            return await send(f"Usage: `{PREFIX}limit <free|premium|dm> <chat|search|browse|panel|mod|announce> <n>`")
        target = cfg["dm_limits"] if p[0] == "dm" else cfg["limits"].setdefault(p[0], {})
        target[p[1]] = max(0, int(p[2])); store.save_cfg()
        return await send(f"✅ {p[0]} {p[1]} limit = {p[2]}/day")
    if cmd == "premium":
        p = arg.split()
        gid = _id(arg)
        if not gid or len(p) < 2 or p[1] not in ("on", "off"):
            return await send(f"Usage: `{PREFIX}premium <guild_id> on|off`")
        store.guild(gid)["tier"] = "premium" if p[1] == "on" else "free"; store.save_guild(gid)
        return await send(f"✅ `{gid}` is now {store.guild(gid)['tier']}")
    if cmd in ("block", "unblock", "blockguild", "unblockguild"):
        uid = _id(arg)
        if not uid:
            return await send(f"Usage: `{PREFIX}{cmd} <id>`")
        lst = cfg["blocked_guilds"] if "guild" in cmd else cfg["blocked"]
        if cmd.startswith("block") and uid not in lst and uid != OWNER_ID:
            lst.append(uid)
            if cmd == "blockguild" and (g := client.get_guild(uid)):
                await g.leave()
        elif cmd.startswith("unblock") and uid in lst:
            lst.remove(uid)
        store.save_cfg()
        return await send(f"✅ {cmd} `{uid}`")
    if cmd in ("pause", "resume"):
        cfg["paused"] = cmd == "pause"; store.save_cfg()
        return await send("⏸️ Paused for everyone but you" if cfg["paused"] else "▶️ Resumed")
    if cmd == "resetall":
        ai.ctx.clear(); return await send("🧹 All chat memory cleared")
    if cmd == "status":
        return await send(
            f"**__BOT_NAME__ status**\nProvider: {ai.provider_name()} · Model: `{cfg['model']}` · fallbacks: {len(cfg['fallbacks'])} · think: {cfg['think']}\n"
            f"Tokens: {cfg['max_tokens']} · temp: {cfg['temperature']} · turns: {cfg['max_turns']}\n"
            f"Servers: {len(client.guilds)} · active chats: {len(ai.ctx)}\n"
            f"Web: {cfg['web']} ({' > '.join(web.active_engines()) or 'no engine'}) · "
            f"searches today: {web.searches_today()}/{cfg['search_cap']}\n"
            f"Browser: {web.browser_name()} · paused: {cfg['paused']} · ping: {round(client.latency * 1000)}ms")
    if cmd == "servers":
        lines = [f"`{g.id}` {g.name} ({g.member_count}) {store.guild(g.id)['tier']}" for g in client.guilds]
        for c in chunks("\n".join(lines) or "None"):
            await send(c)
        return
    if cmd == "leave":
        g = client.get_guild(_id(arg) or 0)
        if not g:
            return await send("Server not found")
        await g.leave(); return await send(f"👋 Left {g.name}")
    if cmd == "sync":
        n = await tree.sync(); return await send(f"✅ Synced {len(n)} slash commands")
    if cmd == "memory":
        sub, _, val = arg.partition(" ")
        sub = sub.lower()
        if sub == "add" and val.strip():
            workspace.add_rule(val)
        elif sub == "del" and val.strip().isdigit():
            if not workspace.del_rule(int(val)):
                return await send("No instruction with that number.")
        elif sub == "clear":
            workspace.save_rules([])
        elif sub:
            return await send(f"Usage: `{PREFIX}memory add <rule>` · `{PREFIX}memory del <n>` · `{PREFIX}memory clear`")
        rules = workspace.rules()
        return await send("**MEMORY.md (standing instructions):**\n" +
                          ("\n".join(f"`{i}.` {r}" for i, r in enumerate(rules, 1)) or "empty"))
    if cmd == "smart":
        if arg:
            cfg["smart_model"] = "" if arg.lower() == "off" else arg
            store.save_cfg()
        return await send(f"Smart model: `{cfg.get('smart_model') or 'off'}` (panel jobs, tickets, long questions, translation)")
    if cmd in ("vision", "audio"):
        key = f"{cmd}_model"
        if arg:
            cfg[key] = "" if arg.lower() == "off" else arg
            store.save_cfg()
        return await send(f"{cmd} backup model: `{cfg[key] or 'off'}` (used only when the main model can't)")
    if cmd == "caps":
        m = arg or cfg["model"]
        mods = await ai.model_inputs(m)
        return await send(f"`{m}` accepts: {', '.join(sorted(mods))}\n"
                          f"Photos: {'✅' if 'image' in mods else ('↪️ ' + cfg['vision_model'] if cfg['vision_model'] else '❌')} · "
                          f"Audio: {'✅' if 'audio' in mods else ('↪️ ' + cfg['audio_model'] if cfg['audio_model'] else '❌')}")
    if cmd == "history":
        cfg["history_lines"] = max(0, min(20, int(arg))); store.save_cfg()
        return await send(f"✅ __BOT_NAME__ reads the last {cfg['history_lines']} channel messages for context")
    if cmd == "clearcache":
        ai.ctx.clear(); ai.clear_caps()
        web._page_cache.clear(); web._search_cache.clear()
        panel.clear_cache(); chat._limit_notice.clear(); workspace.reload()
        return await send("🧹 Cleared chat memory, model info, search/page caches and panel cache")
    if cmd == "serverlog":
        import discord
        from features import serverlog
        await send("📒 Updating the server log…")
        rows = await serverlog.snapshot()
        pub = sum(1 for r in rows if r["invite"])
        lines = [f"`{r['id']}` **{r['name']}** · {r['members']} · " + (f"<{r['invite']}>" if r["invite"] else r["invite_type"])
                 for r in rows[:25]]
        for c in chunks(f"**{len(rows)} servers** ({pub} with invite links)\n" + "\n".join(lines)):
            await send(c)
        return await msg.channel.send(file=discord.File(serverlog.CSV_PATH, filename="__bot_slug__-servers.csv"))
    if cmd == "presence":
        from features import presence
        parts = arg.split()
        p = cfg["presence"]
        if parts:
            kind = parts[0].lower()
            if kind not in ("streaming", "playing", "watching", "listening", "competing", "off"):
                return await send(f"Usage: `{PREFIX}presence streaming|playing|watching|listening|competing|off [twitch/youtube url]`")
            p["type"] = kind
            if len(parts) > 1:
                if kind == "streaming" and not re.match(r"https?://(www\.)?(twitch\.tv|youtube\.com|youtu\.be)/", parts[1]):
                    return await send("Streaming needs a twitch.tv or youtube.com link to show the purple LIVE status.")
                p["url"] = parts[1]
            store.save_cfg()
            await presence.apply()
        return await send(f"🟣 Status: **{p['type']}**" + (f" · {p.get('url')}" if p["type"] == "streaming" else "")
                          + f" · rotates every {p.get('every', 300)}s through {len(p.get('texts', []))} texts")
    if cmd == "statustext":
        from features import presence
        p = cfg["presence"]
        sub, _, val = arg.partition(" ")
        sub = sub.lower()
        if sub == "add" and val.strip():
            p.setdefault("texts", []).append(val.strip()[:128])
        elif sub == "del" and val.strip().isdigit() and 1 <= int(val) <= len(p.get("texts", [])):
            p["texts"].pop(int(val) - 1)
        elif sub == "clear":
            p["texts"] = ["/help"]
        elif sub == "every" and val.strip().isdigit():
            p["every"] = max(60, int(val))
        elif sub:
            return await send(f"Usage: `{PREFIX}statustext add <text>` · `del <n>` · `clear` · `every <seconds>` "
                              "· use `{servers}` / `{users}` for live counts")
        store.save_cfg()
        await presence.apply()
        return await send("**Status texts:**\n" + "\n".join(f"`{i}.` {t}" for i, t in enumerate(p.get("texts", []), 1)))
    if cmd == "version":
        return await send(f"🏷️ Running `{code_version()}` · data folder: `{store.DATA}` · last backup "
                          f"{int(store.last_backup_age() // 60)} min ago")
    if cmd == "backup":
        path = store.backup_db("manual")
        return await send(f"💾 Backup saved: `data/backups/{path.rsplit('/', 1)[-1]}` (keeps the latest {store.KEEP_BACKUPS})")
    if cmd == "reload":
        workspace.reload()
        return await send(f"🔄 Reloaded workspace: {', '.join(workspace.BOOT_FILES)} + {len(workspace.skills())} skills")
    if cmd == "soul":
        text = workspace.read("SOUL.md")
        return await send(f"**SOUL.md** ({len(text)} chars) — edit `workspace/SOUL.md` on the host, then `{PREFIX}reload`.\n"
                          f"```md\n{text[:1500]}\n```")
    if cmd == "skills":
        sk = workspace.skills()
        return await send("**Skills** (workspace/skills/<name>/SKILL.md):\n" +
                          "\n".join(f"• `{n}` — {d}" for n, (d, _) in sk.items()) or "none")
    if cmd == "cleancommands":
        await send("🧹 Removing old slash commands (e.g. from Hermes agent)…")
        removed, n = await chat.clean_old_commands()
        return await send(f"✅ Removed {removed} old server-level commands. {n} __BOT_NAME__ commands active.\n"
                          "-# Discord may need Ctrl+R (or app restart) to refresh the command list.")

