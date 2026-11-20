"""SSH to a server admin's own VPS: safety rules, ready-made templates, and the connection itself.

Safety model
  BLOCKED  never runs (wipes disks, locks people out, opens backdoors, mines crypto, pipes downloads to a shell…)
  READ     looks only (status, logs, disk, ports…): runs straight away
  CHANGE   everything else (restart, install, delete, edit…): needs an admin to press Approve,
           unless the server set mode "auto". "readonly" mode refuses CHANGE completely.
Every command has a time limit and an output cap, and is written to the audit log.
"""
import re, shlex, time, socket, asyncio, ipaddress

from store import env, log

CONNECT_TIMEOUT = 10
RUN_TIMEOUT = int(env("SSH_TIMEOUT", "40") or 40)
MAX_OUTPUT = 3500
ALLOW_PRIVATE = {h.strip().lower() for h in (env("ALLOW_PRIVATE_HOSTS") or "").split(",") if h.strip()}

class SSHError(Exception):
    pass

# =====================================================================
# command safety
# =====================================================================
BLOCKED = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|/\*|~|\$HOME|/(bin|boot|dev|etc|home|lib\w*|opt|root|sbin|srv|usr|var)/?)(\s|$|;|&|\|)",
     "deletes the whole system or a system folder"),
    (r"--no-preserve-root", "deletes the whole system"),
    (r"\bmkfs(\.\w+)?\b|\bwipefs\b|\bfdisk\b|\bparted\b|\bsgdisk\b", "formats or repartitions a disk"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "overwrites a disk"),
    (r">\s*/dev/(sd|nvme|vd|xvd|hd|mmcblk)", "overwrites a disk"),
    (r"\bshred\b[^|;&]*/dev/", "destroys a disk"),
    (r":\s*\(\s*\)\s*\{.*\|.*&\s*\}\s*;", "fork bomb"),
    (r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)?[0-7]*777\s+/(\s|$)|\bchmod\s+-R\b[^;|&]*\s/(\s|$)", "breaks permissions on the whole system"),
    (r"\bchown\s+-R\b[^;|&]*\s/(\s|$)", "changes owner of the whole system"),
    (r"\b(passwd|chpasswd|usermod|userdel|deluser|useradd|adduser|groupmod|visudo|chage)\b", "changes user accounts or passwords"),
    (r"/etc/(shadow|gshadow|sudoers)|authorized_keys|\.ssh/id_|/etc/ssh/sshd_config", "touches logins, passwords or SSH keys"),
    (r"\bsystemctl\s+(stop|disable|mask|kill)\s+(ssh|sshd)(\.service)?\b|\bservice\s+ssh(d)?\s+stop", "would lock everyone out of SSH"),
    (r"\b(iptables|ip6tables)\s+(-F|--flush|-X|-P\s+INPUT\s+DROP)|\bnft\s+flush\b|\bufw\s+(disable|reset|default\s+deny\s+incoming)", "drops the firewall or blocks SSH"),
    (r"\b(curl|wget|fetch)\b[^;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b|\bbash\s+<\(\s*(curl|wget)", "runs a script straight from the internet"),
    (r"\bbase64\s+(-d|--decode)\b[^;&]*\|\s*(ba|z)?sh\b|\beval\b|\bsource\s+/dev/stdin", "runs hidden code"),
    (r"/dev/tcp/|/dev/udp/|\bnc(at)?\b[^;&]*\s-(e|c)\b|\bsocat\b[^;&]*exec|\bmkfifo\b", "opens a remote shell (backdoor)"),
    (r"\b(xmrig|minerd|cpuminer|nicehash|ethminer|t-rex|lolminer)\b|stratum\+tcp", "crypto mining"),
    (r"\bcrontab\s+-r\b", "deletes all scheduled tasks"),
    (r"\bhistory\s+-c\b|>\s*~?/?\.?bash_history|unset\s+HISTFILE|/var/log/(auth|secure|wtmp|btmp|lastlog)\b[^;&]*(>|rm\b|truncate)", "hides activity or deletes security logs"),
    (r"\bkill\s+(-9\s+|-KILL\s+)?1\b|\bkill\s+-9\s+-1\b|\bpkill\s+-9\s+-u\s+root\b", "kills the whole system"),
    (r"\b(init\s+0|telinit\s+0)\b", "turns the server off"),
    (r"\bmount\b[^;&]*-o\s*remount,ro\s+/(\s|$)", "makes the system read-only"),
    (r"\b(insmod|rmmod|modprobe\s+-r)\b", "changes kernel modules"),
    (r"\bsysctl\s+-w\s+kernel\.", "changes kernel settings"),
]

# command words that only look
READ_CMDS = {
    "uptime", "df", "free", "top", "htop", "ps", "pgrep", "pidof", "uname", "hostname", "hostnamectl", "whoami", "id",
    "date", "timedatectl", "lscpu", "lsblk", "lsmem", "lsof", "lspci", "lsusb", "nproc", "vmstat", "iostat", "mpstat",
    "sensors", "dmesg", "journalctl", "ss", "netstat", "ip", "ifconfig", "ping", "traceroute", "tracepath", "mtr",
    "dig", "nslookup", "host", "curl", "wget", "ls", "du", "cat", "head", "tail", "less", "more", "grep", "egrep",
    "zgrep", "find", "stat", "file", "wc", "sort", "uniq", "awk", "cut", "tr", "column", "which", "whereis", "type",
    "last", "lastb", "who", "w", "users", "env", "printenv", "echo", "test", "true", "lsb_release", "getent",
    "openssl", "nginx", "apache2ctl", "apachectl", "php", "python3", "python", "node", "java", "docker", "systemctl",
    "service", "apt", "apt-cache", "dpkg", "rpm", "yum", "dnf", "snap", "certbot", "ufw", "iptables", "nft", "fail2ban-client",
    "crontab", "mysql", "mysqladmin", "redis-cli", "wings", "pm2", "git", "md5sum", "sha256sum", "sar", "uptime",
    "swapon", "mount", "findmnt", "blkid", "smartctl", "nvidia-smi", "ulimit", "sysctl", "tree", "readlink", "realpath",
    "basename", "dirname", "jq", "xargs", "nice", "timeout", "sleep", "seq", "time", "screen", "tmux", "sudo",
}
# sub-commands that change things even though the main word is in READ_CMDS
CHANGE_SUBS = {
    "systemctl": {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "daemon-reload", "kill",
                  "reset-failed", "edit", "set-property", "isolate", "reboot", "poweroff", "halt", "suspend"},
    "service": {"start", "stop", "restart", "reload", "force-reload"},
    "docker": {"run", "start", "stop", "restart", "kill", "rm", "rmi", "prune", "exec", "pull", "build", "update",
               "create", "compose", "cp", "commit", "tag", "push", "connect", "disconnect", "load", "import",
               "pause", "unpause", "rename", "save"},
    "apt": {"install", "remove", "purge", "upgrade", "full-upgrade", "dist-upgrade", "autoremove", "update", "clean",
            "autoclean"},
    "dpkg": {"-i", "--install", "-r", "--remove", "-P", "--purge", "--configure"},
    "yum": {"install", "remove", "update", "upgrade", "erase", "clean"},
    "dnf": {"install", "remove", "update", "upgrade", "erase", "clean"},
    "snap": {"install", "remove", "refresh", "disable", "enable"},
    "certbot": {"renew", "certonly", "delete", "revoke", "--nginx", "--apache", "run", "install"},
    "ufw": {"allow", "deny", "reject", "limit", "delete", "enable", "disable", "reset", "default", "insert", "reload"},
    "iptables": {"-A", "-I", "-D", "-R", "-F", "-X", "-P", "-N", "-Z"},
    "nft": {"add", "delete", "flush", "insert", "replace", "create"},
    "fail2ban-client": {"set", "unban", "reload", "restart", "stop", "start"},
    "crontab": {"-e", "-r", "-"},
    "git": {"pull", "push", "reset", "checkout", "clean", "merge", "rebase", "commit", "stash", "fetch", "clone"},
    "pm2": {"start", "stop", "restart", "reload", "delete", "kill", "flush", "save", "update"},
    "nginx": {"-s"},
    "journalctl": {"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush"},
    "sysctl": {"-w", "-p", "--write", "--load", "--system"},
    "mount": {"-o", "-a", "--bind"},
    "swapon": {"-a"},
    "timedatectl": {"set-time", "set-timezone", "set-ntp"},
    "hostnamectl": {"set-hostname"},
    "mysql": {"-e"},
    "redis-cli": {"flushall", "flushdb", "config", "shutdown", "del", "set"},
    "screen": {"-S", "-dmS", "-X"},
    "tmux": {"new", "kill-session", "kill-server", "send-keys"},
    "wings": {"configure", "diagnostics"},
}
# interpreters can do anything: allow only harmless version/syntax checks without approval
INTERPRETERS = {"python", "python3", "php", "node", "java"}
SAFE_INTERP_ARGS = {"--version", "-V", "-v", "version", "-version", "-m", "-l"}

def _segments(cmd):
    """Split a shell line into simple commands (on ; && || | and newlines), respecting quotes.
    Returns a list of word lists. Raises ValueError on broken quoting."""
    cmd = re.sub(r"\d*>&\d+|&>\s*/dev/null|\d*>>?\s*/dev/null", " ", cmd)   # harmless redirections
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    segs, cur = [], []
    for tok in lex:
        if tok and set(tok) <= set(";&|"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs

def classify(cmd):
    """Return (level, reason): level is 'blocked', 'read' or 'change'."""
    c = cmd.strip()
    if not c:
        return "blocked", "empty command"
    if len(c) > 1500:
        return "blocked", "command too long"
    flat = re.sub(r"\s+", " ", c)
    for pat, why in BLOCKED:
        if re.search(pat, flat, re.I):
            return "blocked", why
    if re.search(r"\b(reboot|shutdown|poweroff|halt)\b", flat):
        return "change", "restarts or turns off the whole VPS"
    if re.search(r"(^|[^0-9&>])>{1,2}\s*[^&\s]|\btee\b|\bsed\s+(-\w*\s+)*-i|\btruncate\b|`|\$\(", flat):
        return "change", "writes to files or runs sub-commands"
    try:
        segments = _segments(c)
    except ValueError:
        return "change", "unusual quoting"
    for words in segments:
        while words and re.match(r"^[A-Z_][A-Z0-9_]*=", words[0]):      # VAR=value prefix
            words = words[1:]
        if words and words[0] in ("sudo", "nice", "timeout", "time", "xargs"):
            words = [w for w in words[1:] if not w.startswith("-") and not re.match(r"^\d+[smh]?$", w)] or words
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        if prog not in READ_CMDS:
            return "change", f"`{prog}` can change things"
        subs = CHANGE_SUBS.get(prog, set())
        args = words[1:]
        if any(a in subs or a.split("=")[0] in subs for a in args):
            return "change", f"`{prog} {next(a for a in args if a in subs or a.split('=')[0] in subs)}` changes things"
        if prog in INTERPRETERS and not (args and args[0] in SAFE_INTERP_ARGS):
            return "change", f"`{prog}` can run any code"
        if prog in ("curl", "wget") and any(a in ("-o", "-O", "--output", "-X", "--data", "-d", "-T", "--upload-file")
                                          or a.startswith(("-o", "--post")) for a in args):
            return "change", "downloads or sends data"
        if prog == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
            return "change", "`find` that deletes or runs commands"
        if prog == "xargs":
            return "change", "`xargs` runs other commands"
        if prog == "sysctl" and any("=" in a for a in args):
            return "change", "`sysctl` that changes kernel settings"
        if prog in ("screen", "tmux") and args:
            return "change", "starts or controls sessions"
    return "read", "only looks"

# =====================================================================
# templates: ready-made checks and fixes
# =====================================================================
TEMPLATES = {
    # ---- checks (read-only) ----
    "health": ("Overall health: load, memory, disk, failed services, top processes",
               "echo '== uptime =='; uptime; echo '== memory =='; free -m; echo '== disk =='; df -h -x tmpfs -x devtmpfs -x overlay; "
               "echo '== failed services =='; systemctl --failed --no-legend --no-pager | head -20; "
               "echo '== top cpu =='; ps -eo pid,user,%cpu,%mem,etime,comm --sort=-%cpu | head -8"),
    "disk": ("What's using disk space",
             "df -h -x tmpfs -x devtmpfs -x overlay; echo; du -xh --max-depth=1 / 2>/dev/null | sort -rh | head -12; "
             "echo; journalctl --disk-usage 2>/dev/null; docker system df 2>/dev/null"),
    "memory": ("Memory and swap, biggest processes",
               "free -m; echo; swapon --show; echo; ps -eo pid,user,%mem,rss,comm --sort=-%mem | head -12"),
    "cpu": ("CPU load and busiest processes",
            "uptime; nproc; echo; top -bn1 | head -20"),
    "services": ("Failed and running services",
                 "systemctl --failed --no-pager; echo; systemctl list-units --type=service --state=running --no-pager --no-legend | head -40"),
    "errors": ("Recent system errors",
               "journalctl -p err -n 60 --no-pager --since '24 hours ago'"),
    "logs": ("Last logs of a service (target = service name)",
             "journalctl -u {target} -n 80 --no-pager"),
    "status": ("Status of a service (target = service name)",
               "systemctl status {target} --no-pager -l | head -40"),
    "ports": ("Open ports and what listens on them",
              "ss -tulpn | head -60"),
    "network": ("IPs, routes, DNS and internet check",
                "ip -br a; echo; ip route | head; echo; cat /etc/resolv.conf | grep -v '^#'; echo; ping -c 3 -W 2 1.1.1.1"),
    "docker": ("Docker containers and their resource use",
               "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | head -40; echo; docker stats --no-stream | head -40"),
    "wings": ("Pterodactyl Wings daemon status and errors",
              "systemctl status wings --no-pager -l | head -25; echo; journalctl -u wings -n 60 --no-pager | grep -iE 'error|fatal|warn|panic' | tail -30"),
    "web": ("Nginx / Apache config test and status",
            "nginx -t 2>&1; systemctl status nginx --no-pager | head -12; apachectl configtest 2>&1 | head -5"),
    "ssl": ("SSL certificates and expiry",
            "certbot certificates 2>/dev/null | grep -E 'Certificate Name|Domains|Expiry' "),
    "security": ("Failed logins, who's logged in, fail2ban",
                 "last -n 10; echo; lastb -n 15 2>/dev/null; echo; journalctl -u ssh -u sshd --since '24 hours ago' --no-pager | grep -ciE 'failed|invalid'; "
                 "fail2ban-client status 2>/dev/null"),
    "updates": ("Pending package updates",
                "apt list --upgradable 2>/dev/null | head -40"),
    "system": ("OS, kernel, CPU, RAM, disks",
               "hostnamectl 2>/dev/null || uname -a; echo; lscpu | grep -E 'Model name|^CPU\\(s\\)'; free -h | head -2; lsblk -d -o NAME,SIZE,MODEL"),
    # ---- fixes (change: need approval unless mode is auto) ----
    "restart": ("Restart a service (target = service name)",
                "systemctl restart {target} && sleep 2 && systemctl status {target} --no-pager | head -12"),
    "restart_docker": ("Restart a Docker container (target = container name)",
                       "docker restart {target} && sleep 2 && docker ps --filter name={target}"),
    "free_disk": ("Free disk space safely: package cache, old logs, unused Docker data",
                  "df -h / | tail -1; apt-get clean; journalctl --vacuum-size=300M; docker system prune -f 2>/dev/null; "
                  "echo '-- after --'; df -h / | tail -1"),
    "free_ram": ("Drop file caches (safe, frees cached RAM)",
                 "free -m | head -2; sync; sysctl -q vm.drop_caches=3; echo '-- after --'; free -m | head -2"),
    "update": ("Install pending security/package updates",
               "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get -y -qq upgrade && echo done"),
    "renew_ssl": ("Renew SSL certificates and reload Nginx",
                  "certbot renew --quiet && systemctl reload nginx && certbot certificates | grep -E 'Domains|Expiry'"),
    "restart_wings": ("Restart Pterodactyl Wings",
                      "systemctl restart wings && sleep 3 && systemctl status wings --no-pager | head -12"),
    "reload_nginx": ("Test and reload Nginx",
                     "nginx -t && systemctl reload nginx && echo reloaded"),
}

def template_cmd(name, target=""):
    name = name.strip().lower().replace("-", "_").replace(" ", "_")
    if name not in TEMPLATES:
        raise SSHError(f"no template `{name}`. Templates: " + ", ".join(TEMPLATES))
    desc, cmd = TEMPLATES[name]
    if "{target}" in cmd:
        target = target.strip()
        if not re.fullmatch(r"[A-Za-z0-9@._:-]{1,80}", target or ""):
            raise SSHError(f"`{name}` needs a service/container name, e.g. `{name} nginx`")
        cmd = cmd.replace("{target}", target)
    return cmd

def template_list():
    return "\n".join(f"`{k}` — {d}" for k, (d, _) in TEMPLATES.items())

# =====================================================================
# connecting
# =====================================================================
def _ip_ok(ip):
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast

async def check_host(host, port, owner=False):
    """Only public addresses (so nobody can reach the bot's own machine or private network)."""
    host = host.strip().lower()
    if not re.fullmatch(r"[a-z0-9.\-:\[\]]{1,253}", host):
        raise SSHError("that doesn't look like a host name or IP")
    if not 1 <= int(port) <= 65535:
        raise SSHError("port must be 1-65535")
    if owner or host in ALLOW_PRIVATE:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host.strip("[]"), int(port), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise SSHError("couldn't find that host")
    if not infos or not all(_ip_ok(i[4][0]) for i in infos):
        raise SSHError("private or local addresses are not allowed")

def _lib():
    try:
        import asyncssh
        return asyncssh
    except ImportError:
        raise SSHError("SSH support isn't installed yet (`asyncssh`). Restart the bot so it installs requirements.")

_locks = {}                                         # one command at a time per Discord server

async def connect(h, secret, owner=False):
    """h: host dict {host, port, user, auth, fingerprint}. Returns (conn, fingerprint)."""
    asyncssh = _lib()
    await check_host(h["host"], h["port"], owner)
    kw = dict(host=h["host"].strip("[]"), port=int(h["port"]), username=h["user"], known_hosts=None,
              connect_timeout=CONNECT_TIMEOUT, login_timeout=CONNECT_TIMEOUT, keepalive_interval=15)
    if h.get("auth") == "key":
        try:
            kw["client_keys"] = [asyncssh.import_private_key(secret)]
        except Exception as e:
            raise SSHError(f"couldn't read that private key ({type(e).__name__}). Paste the whole key, unencrypted.")
        kw["password"] = None
    else:
        kw["password"] = secret
        kw["client_keys"] = None
    try:
        conn = await asyncio.wait_for(asyncssh.connect(**kw), CONNECT_TIMEOUT + 5)
    except asyncio.TimeoutError:
        raise SSHError("connection timed out (is the host/port right and SSH open?)")
    except asyncssh.PermissionDenied:
        raise SSHError("login refused: wrong username, password or key")
    except (OSError, asyncssh.Error) as e:
        raise SSHError(f"couldn't connect: {str(e)[:150]}")
    fp = conn.get_server_host_key().get_fingerprint("sha256")
    if h.get("fingerprint") and h["fingerprint"] != fp:
        conn.close()
        raise SSHError("⚠️ the server's identity (host key) CHANGED since it was added. This can mean the VPS was "
                       "reinstalled — or someone is intercepting the connection. Re-add it with `/ssh add` if you "
                       "trust it.")
    return conn, fp

async def run(gid, h, secret, cmd, owner=False, timeout=None):
    """Run one command. Returns (exit_status, output, seconds)."""
    lock = _locks.setdefault(gid or 0, asyncio.Lock())
    if lock.locked():
        raise SSHError("another SSH command is still running for this server — wait a moment")
    async with lock:
        t0 = time.monotonic()
        conn, _ = await connect(h, secret, owner)
        try:
            r = await asyncio.wait_for(conn.run(cmd, check=False, term_type=None, stdin=None),
                                       timeout or RUN_TIMEOUT)
        except asyncio.TimeoutError:
            raise SSHError(f"command took longer than {timeout or RUN_TIMEOUT}s and was stopped")
        finally:
            conn.close()
        out = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
        out = redact(out)
        if len(out) > MAX_OUTPUT:
            out = out[:1200] + f"\n… ({len(out) - 2400} chars cut) …\n" + out[-1200:]
        return r.exit_status, out or "(no output)", time.monotonic() - t0

SECRET_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)"
    r"|((?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\s*[=:]\s*)\S+"
    r"|\b(ptl[ac]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b", re.I)

def redact(text):
    """Hide passwords, keys and tokens that show up in output before it reaches Discord or the AI."""
    def sub(m):
        if m.group(1):
            return "[private key hidden]"
        if m.group(2):
            return m.group(2) + "[hidden]"
        return "[token hidden]"
    return SECRET_RE.sub(sub, text)

# =====================================================================
# tmate: the client shares a terminal for a while, no password needed
# =====================================================================
TMATE_RE = re.compile(r"(?:ssh\s+(?:-p\s*(\d+)\s+)?)?((?:ro-)?[A-Za-z0-9]{10,64})@([a-z0-9.\-]*tmate\.io)\b(?:\s+-p\s*(\d+))?", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>78DEHMNOZc]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")

def parse_tmate(text):
    """'ssh AbC123@sgp1.tmate.io' -> dict, or None. Read-only (ro-) sessions are refused."""
    m = TMATE_RE.search(text or "")
    if not m:
        return None
    token = m.group(2)
    if token.lower().startswith("ro-"):
        raise SSHError("that's the **read-only** link (starts with `ro-`). Send the other `ssh …@…tmate.io` line.")
    return {"host": m.group(3), "port": int(m.group(1) or m.group(4) or 22), "user": token, "auth": "tmate"}

async def run_tmate(gid, h, cmd, timeout=None):
    """Type a command into a shared tmate terminal and read the result back.
    The output is passed through base64 between markers, so terminal redraws can't garble it."""
    asyncssh = _lib()
    await check_host(h["host"], h["port"])
    lock = _locks.setdefault(gid or 0, asyncio.Lock())
    if lock.locked():
        raise SSHError("another SSH command is still running for this server — wait a moment")
    async with lock:
        t0 = time.monotonic()
        key = asyncssh.generate_private_key("ssh-ed25519")
        try:
            conn = await asyncio.wait_for(asyncssh.connect(
                h["host"], port=h["port"], username=h["user"], known_hosts=None, client_keys=[key],
                password=None, connect_timeout=CONNECT_TIMEOUT, login_timeout=CONNECT_TIMEOUT), CONNECT_TIMEOUT + 5)
        except asyncio.TimeoutError:
            raise SSHError("couldn't reach the tmate session (timed out)")
        except (OSError, asyncssh.Error) as e:
            raise SSHError(f"couldn't join the tmate session — is it still open? ({str(e)[:120]})")
        try:
            proc = await conn.create_process(term_type="vt100", term_size=(200, 60))
            tag = f"{int(time.time() * 1000) % 100000:05d}"
            # quotes split the markers so the typed command line never contains them in one piece
            line = (f"{{ {cmd} ; }} > /tmp/.__bot_slug___{tag} 2>&1; kr=$?; clear; echo \"KB\"\"EG{tag}\"; "
                    f"head -c 6000 /tmp/.__bot_slug___{tag} | base64 -w 150; echo \"KE\"\"ND{tag} $kr\"; "
                    f"rm -f /tmp/.__bot_slug___{tag}\n")
            await asyncio.sleep(1.0)                  # let the shared screen settle
            proc.stdin.write("\x03")                  # cancel anything half-typed in the shared shell
            await asyncio.sleep(0.3)
            proc.stdin.write(line)
            buf, end_re = "", re.compile(rf"KBEG{tag}(.*?)KEND{tag}\s+(\d+)", re.S)
            limit = time.monotonic() + (timeout or RUN_TIMEOUT)
            while time.monotonic() < limit:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), 2)
                except asyncio.TimeoutError:
                    chunk = ""
                if chunk:
                    buf += ANSI_RE.sub("", chunk)
                    if len(buf) > 400_000:
                        buf = buf[-200_000:]
                m = end_re.findall(buf)
                if m:
                    data, code = m[-1]
                    import base64
                    raw = re.sub(r"[^A-Za-z0-9+/=]", "", data)
                    try:
                        out = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace").strip()
                    except Exception:
                        out = "(couldn't read the output back from the shared terminal)"
                    out = redact(out)
                    if len(out) > MAX_OUTPUT:
                        out = out[:1200] + f"\n… ({len(out) - 2400} chars cut) …\n" + out[-1200:]
                    return int(code), out or "(no output)", time.monotonic() - t0
                if proc.exit_status is not None:
                    raise SSHError("the tmate session closed")
            proc.stdin.write("\x03")
            raise SSHError(f"command took longer than {timeout or RUN_TIMEOUT}s; I pressed Ctrl+C in the session")
        finally:
            conn.close()
