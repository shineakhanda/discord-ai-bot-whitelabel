"""Pterodactyl / Reviactyl / Pelican panel client.

Client API (ptlc_ key): servers, power, console, files, startup variables, docker image, reinstall, backups.
Application API (ptla_ key, optional): eggs, egg change, build limits, suspend, admin reinstall.
"""
import re, json, time, asyncio
from urllib.parse import urlparse, quote

import aiohttp

from store import HTTP, guild, decrypt
from web import check_url

class PanelError(Exception):
    pass

def normalize_url(url):
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"

def creds(gid, app=False):
    p = guild(gid)["panel"]
    key = decrypt(p["app_key" if app else "key"]) if p["app_key" if app else "key"] else ""
    if not p["url"]:
        raise PanelError("No panel linked. An admin can link one with `/setup panel`.")
    if not key:
        raise PanelError("This needs the panel's Application API key (ptla_). An admin can add it with `/setup panel`."
                         if app else "No client API key linked. An admin can add it with `/setup panel`.")
    return p["url"], key

def has_app(gid):
    p = guild(gid)["panel"]
    return bool(p["url"] and p["app_key"])

async def _req(url, key, method, path, json_body=None, raw=None, expect_json=True):
    await check_url(url + path)                      # panel must be on a public address
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if raw is None:
        headers["Content-Type"] = "application/json"
    try:
        async with HTTP["s"].request(method, url + path, json=json_body if raw is None else None,
                                     data=raw, headers=headers, allow_redirects=False,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 204:
                return {}
            if r.status in (401, 403):
                raise PanelError("Panel rejected the API key (expired, wrong type, or no access to this).")
            if r.status == 404:
                raise PanelError("Not found on the panel.")
            if r.status == 409:
                raise PanelError("The server is busy (installing, transferring, or suspended).")
            if r.status == 429:
                raise PanelError("Panel rate limit hit, wait a moment.")
            if r.status >= 400:
                try:
                    detail = (await r.json(content_type=None))["errors"][0].get("detail", "")
                except Exception:
                    detail = ""
                raise PanelError(f"Panel error {r.status}. {detail}".strip())
            if not expect_json:
                return await r.text()
            return await r.json(content_type=None)
    except aiohttp.ClientError as e:
        raise PanelError(f"Couldn't reach the panel ({type(e).__name__}).")

async def client(gid, method, path, **kw):
    url, key = creds(gid)
    return await _req(url, key, method, "/api/client" + path, **kw)

async def app(gid, method, path, **kw):
    url, key = creds(gid, app=True)
    return await _req(url, key, method, "/api/application" + path, **kw)

async def test_key(url, key, app_key=False):
    path = "/api/application/nests?per_page=1" if app_key else "/api/client?per_page=1"
    data = await _req(url, key, "GET", path)
    if "data" not in data:
        raise PanelError("That doesn't look like a Pterodactyl API.")

# ---------- servers ----------
_list_cache = {}

async def list_servers(gid, fresh=False):
    """[(identifier, name)] visible to the client key (admins see all servers), cached 60s."""
    hit = _list_cache.get(gid)
    if hit and not fresh and time.time() - hit[0] < 60:
        return hit[1]
    out, seen, page = [], set(), 1
    q = "type=admin-all&" if has_app(gid) else ""
    while page <= 10:
        try:
            data = await client(gid, "GET", f"?{q}page={page}&per_page=50")
        except PanelError:
            if q:
                q = ""
                continue
            raise
        for s in data.get("data", []):
            a = s.get("attributes", {})
            if a.get("identifier") and a["identifier"] not in seen:
                seen.add(a["identifier"])
                out.append((a["identifier"], a.get("name", "?")))
        meta = data.get("meta", {}).get("pagination", {})
        if meta.get("current_page", 1) >= meta.get("total_pages", 1):
            break
        page += 1
    _list_cache[gid] = (time.time(), out)
    return out

def clear_cache(gid=None):
    if gid is None:
        _list_cache.clear(); _app_ids.clear(); _eggs.clear()
    else:
        _list_cache.pop(gid, None); _app_ids.pop(gid, None); _eggs.pop(gid, None)

async def resolve(gid, name_or_id):
    q = (name_or_id or "").strip().strip("`\"'").lower()
    if not q:
        raise PanelError("Which server? Give its name.")
    servers = await list_servers(gid)
    for ident, name in servers:
        if q in (ident.lower(), name.lower()):
            return ident, name
    hits = [(i, n) for i, n in servers if q in n.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise PanelError("Several servers match: " + ", ".join(n for _, n in hits[:8]) + ". Which one?")
    raise PanelError(f"No server named `{name_or_id}` on the panel.")

async def resources(gid, ident):
    a = (await client(gid, "GET", f"/servers/{ident}/resources")).get("attributes", {})
    r = a.get("resources", {})
    return {"state": a.get("current_state", "unknown"), "suspended": a.get("is_suspended", False),
            "cpu": r.get("cpu_absolute", 0), "mem": r.get("memory_bytes", 0), "disk": r.get("disk_bytes", 0),
            "rx": r.get("network_rx_bytes", 0), "tx": r.get("network_tx_bytes", 0), "uptime": r.get("uptime", 0)}

async def details(gid, ident):
    a = (await client(gid, "GET", f"/servers/{ident}")).get("attributes", {})
    lim = a.get("limits", {})
    alloc = next((x.get("attributes", {}) for x in a.get("relationships", {}).get("allocations", {}).get("data", [])
                  if x.get("attributes", {}).get("is_default")), {})
    addr = f"{alloc.get('ip_alias') or alloc.get('ip', '')}:{alloc.get('port', '')}" if alloc else ""
    return {"mem": lim.get("memory", 0), "disk": lim.get("disk", 0), "cpu": lim.get("cpu", 0),
            "node": a.get("node", ""), "address": addr.strip(":"), "uuid": a.get("uuid", ""),
            "backups": a.get("feature_limits", {}).get("backups", 0), "installing": a.get("is_installing", False)}

async def power(gid, ident, signal):
    if signal not in ("start", "stop", "restart", "kill"):
        raise PanelError("Power action must be start, stop, restart or kill.")
    await client(gid, "POST", f"/servers/{ident}/power", json_body={"signal": signal})

async def command(gid, ident, cmd):
    await client(gid, "POST", f"/servers/{ident}/command", json_body={"command": cmd[:500]})

# ---------- console logs via websocket ----------
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")

async def console_logs(gid, ident, wait=4.0, max_lines=120):
    """Recent console output (the panel's 'send logs' + a few seconds of live output)."""
    url, _ = creds(gid)
    info = (await client(gid, "GET", f"/servers/{ident}/websocket")).get("data", {})
    sock, token = info.get("socket"), info.get("token")
    if not sock or not token:
        raise PanelError("Panel didn't give a console connection.")
    await check_url(sock.replace("wss://", "https://").replace("ws://", "http://"))
    lines, status = [], None
    try:
        async with HTTP["s"].ws_connect(sock, origin=url, timeout=10, heartbeat=20) as ws:
            await ws.send_str(json.dumps({"event": "auth", "args": [token]}))
            end = time.time() + wait + 4
            while time.time() < end and len(lines) < max_lines * 2:
                try:
                    msg = await ws.receive(timeout=max(0.2, end - time.time()))
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                ev = json.loads(msg.data)
                name, args = ev.get("event"), ev.get("args") or []
                if name == "auth success":
                    await ws.send_str(json.dumps({"event": "send logs", "args": [None]}))
                    end = time.time() + wait
                elif name in ("console output", "install output", "daemon message") and args:
                    lines.extend(ANSI.sub("", str(args[0])).splitlines())
                elif name == "status" and args:
                    status = args[0]
                elif name in ("jwt error", "token expired"):
                    raise PanelError("Console auth failed.")
    except aiohttp.ClientError as e:
        raise PanelError(f"Couldn't connect to the server console ({type(e).__name__}).")
    lines = [l for l in lines if l.strip()][-max_lines:]
    return status, lines

# ---------- files ----------
MAX_READ = 60_000
MAX_WRITE = 100_000

def _p(path):
    path = "/" + (path or "").strip().lstrip("/")
    if ".." in path.split("/"):
        raise PanelError("Path can't contain '..'.")
    return path

async def list_files(gid, ident, directory="/"):
    data = await client(gid, "GET", f"/servers/{ident}/files/list?directory={quote(_p(directory))}")
    out = []
    for f in data.get("data", []):
        a = f.get("attributes", {})
        out.append((a.get("name", ""), a.get("is_file", True), a.get("size", 0)))
    return out

async def read_file(gid, ident, path):
    text = await client(gid, "GET", f"/servers/{ident}/files/contents?file={quote(_p(path))}", expect_json=False)
    return text[:MAX_READ]

async def write_file(gid, ident, path, content):
    if len(content.encode()) > MAX_WRITE:
        raise PanelError("File too large to write from Discord.")
    await client(gid, "POST", f"/servers/{ident}/files/write?file={quote(_p(path))}", raw=content.encode(),
                 expect_json=False)

async def rename_file(gid, ident, src, dst):
    await client(gid, "PUT", f"/servers/{ident}/files/rename",
                 json_body={"root": "/", "files": [{"from": _p(src).lstrip("/"), "to": _p(dst).lstrip("/")}]})

# ---------- startup / image / reinstall / backups (client API) ----------
async def startup(gid, ident):
    data = await client(gid, "GET", f"/servers/{ident}/startup")
    meta = data.get("meta", {})
    vars_ = [v.get("attributes", {}) for v in data.get("data", [])]
    return {"command": meta.get("startup_command", ""), "images": meta.get("docker_images", {}) or {},
            "vars": [{"key": v.get("env_variable"), "name": v.get("name"), "value": v.get("server_value"),
                      "default": v.get("default_value"), "editable": v.get("is_editable", False),
                      "rules": v.get("rules", "")} for v in vars_]}

async def set_variable(gid, ident, key, value):
    data = await client(gid, "PUT", f"/servers/{ident}/startup/variable", json_body={"key": key, "value": value})
    return data.get("attributes", {}).get("server_value", value)

async def set_image(gid, ident, image):
    await client(gid, "PUT", f"/servers/{ident}/settings/docker-image", json_body={"docker_image": image})

async def reinstall(gid, ident):
    if has_app(gid):
        sid = await app_id(gid, ident)
        await app(gid, "POST", f"/servers/{sid}/reinstall")
    else:
        await client(gid, "POST", f"/servers/{ident}/settings/reinstall")

async def backup(gid, ident, name="__BOT_NAME__ auto-backup"):
    """Create a backup if the server has a free slot. Returns a message."""
    d = await details(gid, ident)
    if not d["backups"]:
        return "no backup slots on this server, so no backup was made"
    existing = (await client(gid, "GET", f"/servers/{ident}/backups")).get("data", [])
    if len(existing) >= d["backups"]:
        return f"backup slots full ({len(existing)}/{d['backups']}), so no new backup was made"
    await client(gid, "POST", f"/servers/{ident}/backups",
                 json_body={"name": f"{name} {time.strftime('%Y-%m-%d %H:%M')}"})
    return "backup started"

# ---------- application API ----------
_app_ids = {}

async def app_id(gid, ident):
    """Internal numeric server id for an identifier."""
    cache = _app_ids.setdefault(gid, {})
    if ident in cache:
        return cache[ident]
    page = 1
    while page <= 20:
        data = await app(gid, "GET", f"/servers?page={page}&per_page=100")
        for s in data.get("data", []):
            a = s.get("attributes", {})
            cache[a.get("identifier")] = a.get("id")
        if ident in cache:
            return cache[ident]
        meta = data.get("meta", {}).get("pagination", {})
        if meta.get("current_page", 1) >= meta.get("total_pages", 1):
            break
        page += 1
    raise PanelError("Server not found with the application key.")

async def app_server(gid, ident):
    sid = await app_id(gid, ident)
    a = (await app(gid, "GET", f"/servers/{sid}")).get("attributes", {})
    return sid, a

_eggs = {}

async def eggs(gid):
    """[{id, name, nest, startup, images, vars:{KEY: default}}], cached 10 min."""
    hit = _eggs.get(gid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = []
    nests = (await app(gid, "GET", "/nests?per_page=100")).get("data", [])
    for n in nests:
        na = n.get("attributes", {})
        data = await app(gid, "GET", f"/nests/{na.get('id')}/eggs?include=variables&per_page=100")
        for e in data.get("data", []):
            ea = e.get("attributes", {})
            variables = {v["attributes"]["env_variable"]: v["attributes"].get("default_value", "")
                         for v in ea.get("relationships", {}).get("variables", {}).get("data", [])}
            images = ea.get("docker_images") or ({"default": ea["docker_image"]} if ea.get("docker_image") else {})
            out.append({"id": ea.get("id"), "name": ea.get("name", "?"), "nest": na.get("name", "?"),
                        "startup": ea.get("startup", ""), "images": images, "vars": variables})
    _eggs[gid] = (time.time(), out)
    return out

async def find_egg(gid, query):
    q = str(query).strip().lower()
    all_eggs = await eggs(gid)
    for e in all_eggs:
        if q == str(e["id"]) or q == e["name"].lower():
            return e
    hits = [e for e in all_eggs if q in e["name"].lower() or q in f"{e['nest']} {e['name']}".lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise PanelError("Several eggs match: " + ", ".join(f"{e['name']} (#{e['id']})" for e in hits[:10]))
    raise PanelError(f"No egg matching `{query}`.")

async def change_egg(gid, ident, egg_query, image=None):
    """Switch the server's egg (keeps matching variable values), then the caller reinstalls."""
    egg = await find_egg(gid, egg_query)
    sid, a = await app_server(gid, ident)
    current = (a.get("container") or {}).get("environment", {}) or {}
    env = {k: (current.get(k) if current.get(k) not in (None, "") else v) for k, v in egg["vars"].items()}
    img = image or (list(egg["images"].values())[0] if egg["images"] else (a.get("container") or {}).get("image"))
    await app(gid, "PATCH", f"/servers/{sid}/startup",
              json_body={"startup": egg["startup"], "environment": env, "egg": egg["id"], "image": img,
                         "skip_scripts": False})
    return egg, img

async def set_build(gid, ident, memory=None, disk=None, cpu=None, swap=None):
    sid, a = await app_server(gid, ident)
    lim, feat = a.get("limits", {}), a.get("feature_limits", {})
    body = {"allocation": a.get("allocation"),
            "memory": memory if memory is not None else lim.get("memory", 0),
            "swap": swap if swap is not None else lim.get("swap", 0),
            "disk": disk if disk is not None else lim.get("disk", 0),
            "io": lim.get("io", 500),
            "cpu": cpu if cpu is not None else lim.get("cpu", 0),
            "threads": lim.get("threads"),
            "feature_limits": {"databases": feat.get("databases", 0), "allocations": feat.get("allocations", 0),
                               "backups": feat.get("backups", 0)}}
    await app(gid, "PATCH", f"/servers/{sid}/build", json_body=body)
    return body

async def suspend(gid, ident, on=True):
    sid = await app_id(gid, ident)
    await app(gid, "POST", f"/servers/{sid}/{'suspend' if on else 'unsuspend'}")

# ---------- formatting ----------
def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024

def fmt_uptime(ms):
    s = int((ms or 0) / 1000)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"

STATE_ICON = {"running": "🟢", "starting": "🟡", "stopping": "🟠", "offline": "🔴"}

# =====================================================================
# hosting-owner level (Application API): users, servers, nodes, locations, abuse scan
# =====================================================================
async def _app_pages(gid, path, max_pages=20):
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while page <= max_pages:
        data = await app(gid, "GET", f"{path}{sep}page={page}&per_page=100")
        out += [d.get("attributes", {}) | {"_rel": d.get("attributes", {}).get("relationships", {})}
                for d in data.get("data", [])]
        meta = data.get("meta", {}).get("pagination", {})
        if meta.get("current_page", 1) >= meta.get("total_pages", 1):
            break
        page += 1
    return out

async def users(gid, query=""):
    q = (query or "").strip()
    if q and "@" in q:
        return await _app_pages(gid, f"/users?filter[email]={quote(q)}", 2)
    if q:
        a = await _app_pages(gid, f"/users?filter[username]={quote(q)}", 2)
        return a or [u for u in await _app_pages(gid, "/users") if q.lower() in
                     f"{u.get('username')} {u.get('email')} {u.get('first_name')} {u.get('last_name')}".lower()]
    return await _app_pages(gid, "/users")

async def find_user(gid, query):
    q = str(query).strip().strip("`")
    if q.isdigit():
        try:
            return (await app(gid, "GET", f"/users/{q}?include=servers")).get("attributes", {})
        except PanelError:
            pass
    found = await users(gid, q)
    exact = [u for u in found if q.lower() in (str(u.get("username", "")).lower(), str(u.get("email", "")).lower())]
    pick = exact or found
    if not pick:
        raise PanelError(f"No panel user matching `{query}`.")
    if len(pick) > 1:
        raise PanelError("Several users match: " + ", ".join(f"{u['username']} (#{u['id']})" for u in pick[:8]))
    return (await app(gid, "GET", f"/users/{pick[0]['id']}?include=servers")).get("attributes", {})

def user_servers(u):
    return [s.get("attributes", {}) for s in (u.get("relationships", {}).get("servers", {}) or {}).get("data", [])]

async def create_user(gid, email, username, first="", last="", password=None):
    body = {"email": email, "username": username, "first_name": first or username, "last_name": last or "User"}
    if password:
        body["password"] = password
    return (await app(gid, "POST", "/users", json_body=body)).get("attributes", {})

async def update_user(gid, u, **changes):
    body = {"email": u["email"], "username": u["username"], "first_name": u.get("first_name") or u["username"],
            "last_name": u.get("last_name") or "User", "language": u.get("language", "en")}
    body.update({k: v for k, v in changes.items() if v is not None})
    return (await app(gid, "PATCH", f"/users/{u['id']}", json_body=body)).get("attributes", {})

async def delete_user(gid, uid):
    await app(gid, "DELETE", f"/users/{uid}")

async def all_servers(gid):
    """Every server on the panel (application view)."""
    return await _app_pages(gid, "/servers?include=user,node")

async def server_rename(gid, ident, name=None, owner_id=None):
    sid, a = await app_server(gid, ident)
    body = {"name": name or a.get("name"), "user": owner_id or a.get("user"),
            "external_id": a.get("external_id"), "description": a.get("description") or ""}
    await app(gid, "PATCH", f"/servers/{sid}/details", json_body=body)

async def server_delete(gid, ident, force=False):
    sid = await app_id(gid, ident)
    await app(gid, "DELETE", f"/servers/{sid}" + ("/force" if force else ""))
    _app_ids.get(gid, {}).pop(ident, None)
    _list_cache.pop(gid, None)

async def locations(gid):
    return await _app_pages(gid, "/locations", 3)

async def server_create(gid, name, owner_id, egg_query, memory, disk, cpu, location=None, backups=1, databases=0):
    egg = await find_egg(gid, egg_query)
    locs = await locations(gid)
    if location:
        loc = next((l for l in locs if str(location).lower() in (str(l.get("id")), str(l.get("short", "")).lower(),
                                                                  str(l.get("long", "")).lower())), None)
        if not loc:
            raise PanelError(f"No location `{location}`. Have: " + ", ".join(str(l.get("short")) for l in locs))
        loc_ids = [loc["id"]]
    else:
        loc_ids = [l["id"] for l in locs]
    image = list(egg["images"].values())[0] if egg["images"] else ""
    body = {"name": name[:190], "user": owner_id, "egg": egg["id"], "docker_image": image, "startup": egg["startup"],
            "environment": {k: (v if v not in (None, "") else "latest") for k, v in egg["vars"].items()},
            "limits": {"memory": memory, "swap": 0, "disk": disk, "io": 500, "cpu": cpu},
            "feature_limits": {"databases": databases, "backups": backups, "allocations": 0},
            "deploy": {"locations": loc_ids, "dedicated_ip": False, "port_range": []}, "start_on_completion": True}
    a = (await app(gid, "POST", "/servers", json_body=body)).get("attributes", {})
    _list_cache.pop(gid, None)
    return a, egg

async def nodes(gid):
    return await _app_pages(gid, "/nodes?include=location", 3)

async def node_maintenance(gid, query, on):
    ns = await nodes(gid)
    q = str(query).strip().lower()
    n = next((x for x in ns if q in (str(x.get("id")), str(x.get("name", "")).lower(), str(x.get("fqdn", "")).lower())), None)
    if not n:
        raise PanelError(f"No node `{query}`. Nodes: " + ", ".join(x.get("name", "?") for x in ns))
    body = {k: n.get(k) for k in ("name", "description", "location_id", "fqdn", "scheme", "behind_proxy", "public",
                                  "memory", "memory_overallocate", "disk", "disk_overallocate", "upload_size",
                                  "daemon_sftp", "daemon_listen", "daemon_base")}
    body["maintenance_mode"] = bool(on)
    await app(gid, "PATCH", f"/nodes/{n['id']}", json_body={k: v for k, v in body.items() if v is not None})
    return n

MINER = re.compile(r"(xmrig|xmr-stak|minerd|cpuminer|cgminer|bfgminer|ethminer|nbminer|t-rex|lolminer|phoenixminer|"
                   r"nicehash|stratum\+tcp|stratum\+ssl|monero|cryptonight|randomx|hashrate|pool\.minexmr|"
                   r"supportxmr|2miners|unmineable)", re.I)

async def scan(gid, max_servers=80):
    """Look for abuse: miner keywords in startup/env/name, and live CPU/RAM hogs. Returns dict."""
    servers = await all_servers(gid)
    flagged = []
    for s in servers:
        cont = s.get("container") or {}
        blob = " ".join([s.get("name", ""), s.get("description") or "", cont.get("startup_command") or "",
                         json.dumps(cont.get("environment") or {})])
        m = MINER.search(blob)
        if m:
            flagged.append((s, f"miner keyword `{m.group()}`"))
    live = []
    sem = asyncio.Semaphore(5)

    async def one(s):
        async with sem:
            try:
                r = await resources(gid, s["identifier"])
                lim = (s.get("limits") or {})
                pct = r["cpu"] / lim["cpu"] * 100 if lim.get("cpu") else r["cpu"]
                live.append((s, r, pct))
            except Exception:
                pass
    await asyncio.gather(*(one(s) for s in servers[:max_servers] if not s.get("suspended")))
    live.sort(key=lambda x: x[2], reverse=True)
    return {"total": len(servers), "suspended": sum(1 for s in servers if s.get("suspended")),
            "flagged": flagged, "live": live, "scanned": min(len(servers), max_servers)}
