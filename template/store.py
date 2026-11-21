"""Env, global config (owner), per-server settings (admins), daily quotas, secret encryption."""
import os, json, time, copy, sqlite3, logging

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------- env ----------
def load_env(path):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env(os.path.join(BASE, ".env"))

def env(name, default=""):
    return os.getenv(name, default).strip()

DISCORD_TOKEN = env("DISCORD_TOKEN")
OR_KEY = env("OPENROUTER_API_KEY")
# Main AI provider: any OpenAI-compatible API (e.g. numes.ai, DeepSeek, Groq…). Empty = OpenRouter.
AI_BASE_URL = env("AI_BASE_URL").rstrip("/")
AI_API_KEY = env("AI_API_KEY")
# numes.ai shortcut: just set NUMES_API_KEY (free DeepSeek V4.1 Flash by default)
NUMES_API_KEY = env("NUMES_API_KEY")
NUMES_BASE = "https://api.numes.ai/api/v1"
NUMES_MODEL = env("NUMES_MODEL") or "deepseek/deepseek-v4.1-flash:free"
if NUMES_API_KEY and not AI_BASE_URL:
    AI_BASE_URL, AI_API_KEY = NUMES_BASE, NUMES_API_KEY
USING_NUMES = AI_BASE_URL == NUMES_BASE
# Google AI Studio (free Gemma 4). Models named "g:<id>" go here. When the key is set, Gemma becomes the main model.
GOOGLE_AI_KEY = env("GOOGLE_AI_KEY") or env("GEMINI_API_KEY")
GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GOOGLE_MODEL = env("GOOGLE_MODEL") or "gemma-4-31b-it"
OWNER_ID = int(env("OWNER_ID", "0") or 0)
PREFIX = env("PREFIX", "!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("__bot_slug__")

# ---------- global config (owner only) ----------
# ---------- data folder: everything that changes at runtime lives here (never in git) ----------
DATA = env("DATA_DIR") or os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

def _move_into_data(name):
    old, new = os.path.join(BASE, name), os.path.join(DATA, name)
    if os.path.exists(old) and not os.path.exists(new):
        os.replace(old, new)
        log.info("moved %s into data/", name)

for _n in ("__bot_slug__.db", "__bot_slug__.db-wal", "__bot_slug__.db-shm", "config.json", ".secret_key"):
    _move_into_data(_n)

CONFIG_FILE = os.path.join(DATA, "config.json")
DEFAULTS = {
    "model": env("DEFAULT_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    "fallbacks": [m.strip() for m in env("FALLBACK_MODELS").split(",") if m.strip()],
    "max_tokens": 1200,
    "temperature": 0.7,
    "max_turns": 12,
    "think": "off",
    "hedge": float(env("SWITCH_AFTER", "20") or 20),   # seconds a model may stay silent before the next one is asked (0 = wait)
    "vision_model": env("VISION_MODEL"),      # used for photos if the main model can't see images
    "audio_model": env("AUDIO_MODEL"),        # used for voice messages if the main model can't hear
    "history_lines": 10,                      # recent channel messages __BOT_NAME__ reads for context (0 = off)
    "smart_model": env("SMART_MODEL"),        # stronger model for admin jobs, tickets, long questions, translation
    "human_delay": True,                      # short "typing" pause before replies, feels more human
    "human_split": True,                      # short multi-paragraph replies go out as 2-3 messages
    "name_trigger": True,                     # answer when someone writes "__bot_slug__" in a message
    "presence": {"type": "streaming", "url": env("STREAM_URL", "__STREAM_URL__"),
                 "texts": ["/help · {servers} servers", "Bangla · Banglish · English · हिन्दी",
                           "__WEBSITE__"], "every": 300},
    "paused": False,
    "web": "auto",                       # off | manual | auto
    "engines": [e.strip() for e in env("SEARCH_ORDER", "google,serper,tavily,ddg").split(",") if e.strip()],
    "search_cap": 300,                   # global searches/day across all servers (protects API keys)
    "blocked": [],                       # user ids
    "blocked_guilds": [],
    # daily limits per server, by tier. Premium is ready for later.
    "limits": {
        "free":    {"chat": 300,  "search": 20,  "browse": 15,  "panel": 150,  "mod": 150,  "announce": 20, "ssh": 40},
        "premium": {"chat": 3000, "search": 200, "browse": 150, "panel": 1500, "mod": 1500, "announce": 200, "ssh": 400},
    },
    "dm_limits": {"chat": 60, "search": 5, "browse": 5, "panel": 0, "mod": 0, "announce": 0, "ssh": 0},   # per user in DMs
    "user_daily_chat": 80,               # per user per day across all servers (anti-abuse)
}

def _load_cfg():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            saved = json.load(open(CONFIG_FILE, encoding="utf-8"))
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    _deep_merge(cfg[k], v)
                else:
                    cfg[k] = v
        except Exception as e:
            log.warning("config load failed: %s", e)
    return cfg

def _deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst

cfg = _load_cfg()

# one-time upgrades of old saved settings (config.json keeps old values forever otherwise)
if cfg.get("cfg_version", 1) < 2:
    cfg["max_tokens"] = max(cfg.get("max_tokens", 0), 1200)      # 400 cut answers short and squeezed the layout
    cfg["max_turns"] = max(cfg.get("max_turns", 0), 12)
    cfg["history_lines"] = max(cfg.get("history_lines", 0), 10)
    cfg["cfg_version"] = 2
    _save_cfg_quiet = True
else:
    _save_cfg_quiet = False
if cfg.get("cfg_version", 1) < 3:          # v3: Gemma is the default brain everywhere (Gemini only if chosen)
    cfg["brain"] = "gemma"
    cfg["cfg_version"] = 3
    _save_cfg_quiet = True

def _switch_provider():
    """When the main AI provider changes (e.g. OpenRouter -> numes.ai), pick a model that exists there.
    OpenRouter backups get the "or:" prefix so they keep working as fallbacks."""
    if cfg.get("provider", "") == AI_BASE_URL:
        return False
    def to_or(m):
        return m if (not m or m.startswith("or:") or not OR_KEY) else "or:" + m
    if AI_BASE_URL:                                   # moving to another provider
        old = [cfg.get("model", "")] + list(cfg.get("fallbacks", []))
        cfg["model"] = NUMES_MODEL if USING_NUMES else (env("DEFAULT_MODEL") or cfg["model"])
        backups = (["deepseek/deepseek-v4.1-flash-1017:free"] if USING_NUMES else []) + \
                  [to_or(m) for m in old if m and OR_KEY]
        cfg["fallbacks"] = list(dict.fromkeys(b for b in backups if b and b != cfg["model"]))[:4]
        for k in ("smart_model", "vision_model", "audio_model"):
            cfg[k] = to_or(cfg.get(k, ""))
    else:                                             # back to OpenRouter
        cfg["model"] = env("DEFAULT_MODEL", DEFAULTS["model"])
        cfg["fallbacks"] = [m.removeprefix("or:") for m in cfg.get("fallbacks", [])
                            if not m.startswith("deepseek/deepseek-v4.1-flash")
                            and m.removeprefix("or:") != cfg["model"]]
        for k in ("smart_model", "vision_model", "audio_model"):
            cfg[k] = (cfg.get(k) or "").removeprefix("or:")
    cfg["provider"] = AI_BASE_URL
    log.info("AI provider changed -> %s, model %s", AI_BASE_URL or "OpenRouter", cfg["model"])
    return True

def _switch_google():
    """GOOGLE_AI_KEY added -> Gemma main, old main kept as first backup. Key removed -> undo."""
    want = ("g:" + GOOGLE_MODEL.removeprefix("g:")) if GOOGLE_AI_KEY else ""
    if cfg.get("google_main", "") == want:
        return False
    fb = [m for m in cfg.get("fallbacks", []) if not m.startswith("g:")]
    if want:
        old = cfg.get("model", "")
        if old and not old.startswith("g:"):
            fb.insert(0, old)
        cfg["model"] = want
        backup = "g:gemma-4-26b-a4b-it" if "26b" not in want else "g:gemma-4-31b-it"
        gemma = [backup] + (["or:google/gemma-4-26b-a4b-it:free", "or:google/gemma-4-31b-it:free"] if OR_KEY else [])
        small = []                                 # Gemma 3 is no longer on Google's API / OpenRouter free
        # OpenRouter answers fast; free numes.ai queues are slow, so they go last
        fb = [m for m in fb if m.startswith("or:")] + [m for m in fb if not m.startswith("or:")]
        cfg["fallbacks"] = [m for m in dict.fromkeys(gemma + fb[:1] + small + fb[1:]) if m != want][:12]
    else:
        if cfg.get("model", "").startswith("g:"):
            cfg["model"] = fb.pop(0) if fb else DEFAULTS["model"]
        cfg["fallbacks"] = fb
    cfg["google_main"] = want
    log.info("Google AI %s, model %s", "on" if want else "off", cfg["model"])
    return True

_changed = _switch_provider()
_changed = _switch_google() or _changed
if _changed:
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        json.dump(cfg, open(tmp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        log.warning("config save failed: %s", e)

def save_cfg():
    tmp = CONFIG_FILE + ".tmp"
    json.dump(cfg, open(tmp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)

# ---------- per-server settings (server admins) ----------
GUILD_DEFAULTS = {
    "tier": "free",
    "type": "general",                   # general | community | support | store | game | minecraft | hosting
    "lang": "auto",                      # auto | bn | banglish | en
    "auto_channels": [],
    "features": {"chat": True, "search": True, "browse": True, "panel": True, "tickets": True},
    "knowledge": "",                     # admin-written server info, used in answers
    "knowledge_scope": "all",            # all | tickets
    "info_channels": [],                 # channel ids the bot always reads (pricing, rules, FAQ…), /setup infochannels
    "tickets": {"enabled": False, "roles": [], "category": None, "handoff": True},
    # key = client API key (ptlc_), app_key = application API key (ptla_), both encrypted
    "panel": {"url": "", "key": "", "app_key": "", "role": None, "admin_role": None, "console": False,
              "log_channel": None, "auto_backup": True,
              "owner_role": None,                         # hosting owners: users/servers/nodes (Application API)
              "watch": False, "watch_cpu": 95, "watch_suspend": False},
    "timezone": "Asia/Dhaka",
    # VPS over SSH: hosts = {name: {host, port, user, auth, secret(encrypted), fingerprint}}
    "ssh": {"hosts": {}, "role": None, "log_channel": None, "mode": "approve"},   # readonly | approve | auto
    "announce": {"role": None, "channel": None},          # who may announce (plus Manage Server), default channel
    "mod": {"enabled": False, "log_channel": None, "mod_roles": [], "strictness": "medium",
            "delete": True, "timeout": True, "dm_warn": True, "invites": False,
            "exempt_roles": [], "exempt_channels": [], "words": [],
            "links": "allow", "allow_domains": ["youtube.com", "youtu.be", "tenor.com", "github.com"],
            "raid_joins": 8, "raid_action": "alert", "raid_prev_level": None,
            "min_age_days": 0, "new_account_action": "alert", "digest": True},
    "welcome": {"enabled": False, "channel": None, "note": ""},
    "support_invite": {"enabled": False, "code": ""},     # admins allow the bot owner a support invite
}

DB_FILE = os.path.join(DATA, "__bot_slug__.db")
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS guilds (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (scope TEXT, day TEXT, kind TEXT, n INTEGER, PRIMARY KEY (scope, day, kind));
CREATE TABLE IF NOT EXISTS tickets (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, opener INTEGER,
                                    active INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
                                     reason TEXT, by_id INTEGER, ts REAL);
CREATE INDEX IF NOT EXISTS warn_user ON warnings (guild_id, user_id);
CREATE TABLE IF NOT EXISTS cases (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
                                  kind TEXT, reason TEXT, by_id INTEGER, ts REAL);
CREATE INDEX IF NOT EXISTS case_user ON cases (user_id, ts);
CREATE INDEX IF NOT EXISTS case_guild ON cases (guild_id, ts);
CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
                                      text TEXT, next_run REAL, every INTEGER, ping TEXT, by_id INTEGER);
CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS mem_user ON memories (user_id);
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel_id INTEGER,
                                      guild_id INTEGER, due REAL, text TEXT);
CREATE INDEX IF NOT EXISTS rem_due ON reminders (due);
CREATE TABLE IF NOT EXISTS chat_ctx (key INTEGER, ts REAL, role TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS chat_summary (key INTEGER PRIMARY KEY, text TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS chat_ctx_key ON chat_ctx (key, ts);
""")
db.commit()

_gcache = {}

def guild(gid):
    """Settings for a server (merged with defaults). Mutate then call save_guild(gid)."""
    if gid in _gcache:
        return _gcache[gid]
    row = db.execute("SELECT data FROM guilds WHERE id=?", (gid,)).fetchone()
    data = copy.deepcopy(GUILD_DEFAULTS)
    if row:
        try:
            _deep_merge(data, json.loads(row[0]))
        except Exception:
            pass
    _gcache[gid] = data
    return data

def save_guild(gid):
    db.execute("INSERT OR REPLACE INTO guilds (id, data) VALUES (?, ?)",
               (gid, json.dumps(_gcache[gid], ensure_ascii=False)))
    db.commit()

def delete_guild(gid):
    _gcache.pop(gid, None)
    db.execute("DELETE FROM guilds WHERE id=?", (gid,))
    db.execute("DELETE FROM tickets WHERE guild_id=?", (gid,))
    db.execute("DELETE FROM schedules WHERE guild_id=?", (gid,))
    db.execute("DELETE FROM warnings WHERE guild_id=?", (gid,))
    db.execute("DELETE FROM cases WHERE guild_id=?", (gid,))
    db.commit()

# ---------- daily quotas ----------
def _day():
    return time.strftime("%Y-%m-%d", time.gmtime())

def used(scope, kind):
    row = db.execute("SELECT n FROM usage WHERE scope=? AND day=? AND kind=?", (scope, _day(), kind)).fetchone()
    return row[0] if row else 0

def take(scope, kind, limit):
    """Use 1 unit if under limit. Returns True if allowed."""
    if limit is None:
        return True
    if used(scope, kind) >= limit:
        return False
    db.execute("INSERT INTO usage (scope, day, kind, n) VALUES (?, ?, ?, 1) "
               "ON CONFLICT(scope, day, kind) DO UPDATE SET n = n + 1", (scope, _day(), kind))
    db.commit()
    return True

def limit_for(gid, kind):
    if gid is None:
        return cfg["dm_limits"].get(kind, 0)
    tier = guild(gid).get("tier", "free")
    return cfg["limits"].get(tier, cfg["limits"]["free"]).get(kind, 0)

def quota(gid, user_id, kind):
    """Check server (or DM user) quota for kind. Chat also checks per-user daily cap."""
    if user_id == OWNER_ID:
        return True
    if kind == "chat" and not take(f"u:{user_id}:all", "chat", cfg["user_daily_chat"]):
        return False
    scope = f"g:{gid}" if gid else f"u:{user_id}"
    return take(scope, kind, limit_for(gid, kind))

def prune_usage():
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 86400))
    db.execute("DELETE FROM usage WHERE day < ?", (cutoff,))
    db.execute("DELETE FROM tickets WHERE ts < ?", (time.time() - 14 * 86400,))
    db.execute("DELETE FROM warnings WHERE ts < ?", (time.time() - 90 * 86400,))
    db.execute("DELETE FROM cases WHERE ts < ?", (time.time() - 180 * 86400,))
    db.commit()

# ---------- tickets ----------
def ticket_get(cid):
    r = db.execute("SELECT guild_id, opener, active FROM tickets WHERE channel_id=?", (cid,)).fetchone()
    return {"guild": r[0], "opener": r[1], "active": bool(r[2])} if r else None

def ticket_set(cid, gid, opener, active=True):
    db.execute("INSERT OR REPLACE INTO tickets VALUES (?, ?, ?, ?, ?)", (cid, gid, opener, int(active), time.time()))
    db.commit()

def ticket_active(cid, active):
    db.execute("UPDATE tickets SET active=?, ts=? WHERE channel_id=?", (int(active), time.time(), cid))
    db.commit()

def ticket_delete(cid):
    db.execute("DELETE FROM tickets WHERE channel_id=?", (cid,))
    db.commit()

# ---------- secret encryption (panel API keys) ----------
from cryptography.fernet import Fernet, InvalidToken

def _fernet():
    key = env("SECRET_KEY")
    if not key:
        path = os.path.join(DATA, ".secret_key")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(Fernet.generate_key())
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            log.warning("Generated .secret_key — back it up; without it saved panel keys can't be read.")
        key = open(path, "rb").read().strip().decode()
    return Fernet(key.encode())

_F = _fernet()

def encrypt(text):
    return _F.encrypt(text.encode()).decode()

def decrypt(token):
    try:
        return _F.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""

# ---------- user memories (facts users ask __BOT_NAME__ to remember) ----------
MAX_MEMORIES = 40
MEM_LEN = 200

def mem_list(uid):
    return db.execute("SELECT id, text FROM memories WHERE user_id=? ORDER BY id", (uid,)).fetchall()

def mem_add(uid, text):
    text = " ".join(text.split())[:MEM_LEN]
    rows = mem_list(uid)
    if any(t.lower() == text.lower() for _, t in rows):
        return "already saved"
    if len(rows) >= MAX_MEMORIES:                       # drop the oldest
        db.execute("DELETE FROM memories WHERE id=?", (rows[0][0],))
    db.execute("INSERT INTO memories (user_id, text, ts) VALUES (?, ?, ?)", (uid, text, time.time()))
    db.commit()
    return "saved"

def mem_forget(uid, words):
    """Delete memories matching words (or a 1-based number). Returns count."""
    rows = mem_list(uid)
    w = words.strip().lower()
    if w.isdigit() and 1 <= int(w) <= len(rows):
        ids = [rows[int(w) - 1][0]]
    else:
        ids = [i for i, t in rows if w and w in t.lower()]
    for i in ids:
        db.execute("DELETE FROM memories WHERE id=?", (i,))
    db.commit()
    return len(ids)

def mem_clear(uid):
    db.execute("DELETE FROM memories WHERE user_id=?", (uid,))
    db.commit()

# ---------- reminders ----------
MAX_REMINDERS = 10

def rem_add(uid, cid, gid, due, text):
    n = db.execute("SELECT COUNT(*) FROM reminders WHERE user_id=?", (uid,)).fetchone()[0]
    if n >= MAX_REMINDERS:
        return None
    cur = db.execute("INSERT INTO reminders (user_id, channel_id, guild_id, due, text) VALUES (?, ?, ?, ?, ?)",
                     (uid, cid, gid, due, text[:300]))
    db.commit()
    return cur.lastrowid

def rem_list(uid):
    return db.execute("SELECT id, due, text FROM reminders WHERE user_id=? ORDER BY due", (uid,)).fetchall()

def rem_cancel(uid, rid):
    n = db.execute("DELETE FROM reminders WHERE user_id=? AND id=?", (uid, rid)).rowcount
    db.commit()
    return n

def rem_due():
    return db.execute("SELECT id, user_id, channel_id, guild_id, text FROM reminders WHERE due<=? LIMIT 20",
                      (time.time(),)).fetchall()

def rem_done(rid):
    db.execute("DELETE FROM reminders WHERE id=?", (rid,))
    db.commit()

# ---------- warnings ----------
def warn_add(gid, uid, reason, by_id):
    db.execute("INSERT INTO warnings (guild_id, user_id, reason, by_id, ts) VALUES (?, ?, ?, ?, ?)",
               (gid, uid, reason[:300], by_id, time.time()))
    db.commit()

def warn_list(gid, uid, since=0):
    return db.execute("SELECT id, reason, by_id, ts FROM warnings WHERE guild_id=? AND user_id=? AND ts>=? "
                      "ORDER BY ts DESC", (gid, uid, since)).fetchall()

def warn_clear(gid, uid):
    n = db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (gid, uid)).rowcount
    db.commit()
    return n

# ---------- moderation cases ----------
def case_add(gid, uid, kind, reason, by_id):
    cur = db.execute("INSERT INTO cases (guild_id, user_id, kind, reason, by_id, ts) VALUES (?, ?, ?, ?, ?, ?)",
                     (gid, uid, kind, (reason or "")[:300], by_id, time.time()))
    db.commit()
    return cur.lastrowid

def case_list(gid, uid, limit=25):
    return db.execute("SELECT id, kind, reason, by_id, ts FROM cases WHERE guild_id=? AND user_id=? ORDER BY ts DESC "
                      "LIMIT ?", (gid, uid, limit)).fetchall()

def case_recent(gid, since):
    return db.execute("SELECT id, user_id, kind, reason, by_id, ts FROM cases WHERE guild_id=? AND ts>=? ORDER BY ts",
                      (gid, since)).fetchall()

def case_last_guild(uid):
    r = db.execute("SELECT guild_id FROM cases WHERE user_id=? ORDER BY ts DESC LIMIT 1", (uid,)).fetchone()
    return r[0] if r else None

# ---------- schedules ----------
MAX_SCHEDULES = 25

def sched_add(gid, cid, text, next_run, every, ping, by_id):
    n = db.execute("SELECT COUNT(*) FROM schedules WHERE guild_id=?", (gid,)).fetchone()[0]
    if n >= MAX_SCHEDULES:
        return None
    cur = db.execute("INSERT INTO schedules (guild_id, channel_id, text, next_run, every, ping, by_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (gid, cid, text[:1900], next_run, every, ping, by_id))
    db.commit()
    return cur.lastrowid

def sched_list(gid):
    return db.execute("SELECT id, channel_id, text, next_run, every, ping FROM schedules WHERE guild_id=? "
                      "ORDER BY next_run", (gid,)).fetchall()

def sched_cancel(gid, sid):
    n = db.execute("DELETE FROM schedules WHERE guild_id=? AND id=?", (gid, sid)).rowcount
    db.commit()
    return n

def sched_due():
    return db.execute("SELECT id, guild_id, channel_id, text, every, ping, by_id FROM schedules WHERE next_run<=? "
                      "LIMIT 20", (time.time(),)).fetchall()

def sched_done(sid, every):
    if every:
        db.execute("UPDATE schedules SET next_run = next_run + ? * (1 + CAST((? - next_run) / ? AS INTEGER)) "
                   "WHERE id=?", (every, time.time(), every, sid))
    else:
        db.execute("DELETE FROM schedules WHERE id=?", (sid,))
    db.commit()

# ---------- user profiles (personalization) ----------
PROFILE_KEYS = ("nickname", "language", "style")

def profile(uid):
    r = db.execute("SELECT data FROM profiles WHERE user_id=?", (uid,)).fetchone()
    try:
        return json.loads(r[0]) if r else {}
    except Exception:
        return {}

def profile_set(uid, **kw):
    p = profile(uid)
    for k, v in kw.items():
        if k in PROFILE_KEYS:
            if v:
                p[k] = str(v)[:60]
            else:
                p.pop(k, None)
    db.execute("INSERT OR REPLACE INTO profiles (user_id, data) VALUES (?, ?)", (uid, json.dumps(p, ensure_ascii=False)))
    db.commit()
    return p

def profile_clear(uid):
    db.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
    db.commit()

# ---------- schema upgrades (safe to run every start; never drops data) ----------
def ensure_column(table, column, decl):
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        db.commit()
        log.info("database: added %s.%s", table, column)

# add future columns here, e.g. ensure_column("reminders", "repeat", "INTEGER DEFAULT 0")

# ---------- backups ----------
BACKUP_DIR = os.path.join(DATA, "backups")
KEEP_BACKUPS = 14

def backup_db(tag="daily"):
    """Consistent copy of the live database (safe while the bot runs). Returns the file path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, f"__bot_slug__-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{tag}.db")
    out = sqlite3.connect(dst)
    try:
        db.backup(out)
    finally:
        out.close()
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    for f in files[:-KEEP_BACKUPS]:
        os.remove(os.path.join(BACKUP_DIR, f))
    return dst

def last_backup_age():
    try:
        files = [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.endswith(".db")]
        return time.time() - max(os.path.getmtime(f) for f in files)
    except (OSError, ValueError):
        return float("inf")

# a backup on every start (an update is a restart), at most once an hour so crash loops don't fill the disk
if last_backup_age() > 3600:
    try:
        log.info("startup backup: %s", os.path.basename(backup_db("startup")))
    except Exception as e:
        log.warning("startup backup failed: %s", e)

def close_db():
    try:
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
    except Exception:
        pass

# shared aiohttp session (set by bot on startup)
HTTP = {"s": None}


# ---------- conversation memory (survives restarts / updates) ----------
def ctx_load(key, since, limit):
    rows = db.execute("SELECT ts, role, content FROM chat_ctx WHERE key=? AND ts>=? ORDER BY ts DESC, rowid DESC "
                      "LIMIT ?", (key, since, limit)).fetchall()
    return list(reversed(rows))

def ctx_add(key, ts, role, content):
    db.execute("INSERT INTO chat_ctx (key, ts, role, content) VALUES (?, ?, ?, ?)", (key, ts, role, content[:6000]))
    db.commit()

def ctx_clear(key):
    db.execute("DELETE FROM chat_ctx WHERE key=?", (key,))
    db.commit()

def ctx_prune(before):
    db.execute("DELETE FROM chat_ctx WHERE ts<?", (before,))
    db.commit()

if _save_cfg_quiet:
    try:
        save_cfg()
        log.info("settings upgraded: answer length %s tokens, %s chat turns, %s context lines",
                 cfg["max_tokens"], cfg["max_turns"], cfg["history_lines"])
    except Exception as e:
        log.warning("config upgrade save failed: %s", e)


def summary_get(key):
    row = db.execute("SELECT text FROM chat_summary WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""

def summary_set(key, text):
    db.execute("INSERT OR REPLACE INTO chat_summary (key, text, ts) VALUES (?, ?, ?)", (key, text[:2000], time.time()))
    db.commit()
