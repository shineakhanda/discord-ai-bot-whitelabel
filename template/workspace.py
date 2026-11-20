"""OpenClaw-style workspace: SOUL / IDENTITY / AGENTS / TOOLS / MEMORY files, skills, daily notes, prompt assembly."""
import os, re, time

from store import BASE, DATA, guild, log

WS = os.path.join(BASE, "workspace")
SKILLS_DIR = os.path.join(WS, "skills")
NOTES_DIR = os.path.join(DATA, "notes")            # daily audit notes (runtime data, not in git)
BOOT_FILES = ["IDENTITY.md", "SOUL.md", "AGENTS.md", "TOOLS.md"]
os.makedirs(NOTES_DIR, exist_ok=True)

_cache = {}

def read(name):
    """Workspace file text without '#'-comment-only header lines, cached by mtime."""
    path = name if os.path.isabs(name) else os.path.join(WS, name)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return ""
    hit = _cache.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    text = open(path, encoding="utf-8").read()
    text = "\n".join(l for l in text.splitlines() if not l.startswith("(Only the bot owner") and
                     not l.startswith("(Loaded every"))
    _cache[path] = (mt, text.strip())
    return _cache[path][1]

def reload():
    _cache.clear()
    _skills.clear()

# ---------- MEMORY.md (standing instructions) ----------
MEMORY = os.path.join(DATA, "MEMORY.md")            # edited at runtime (!memory, save_instruction) → data/
MEM_HEADER = ("# MEMORY.md — standing instructions and long-term notes\n"
              "# One instruction per line starting with \"- \". __BOT_NAME__ follows these everywhere.\n"
              "# Loaded in every server, so never put secrets or private info here.\n"
              "# Edit this file, use !memory add / !memory del <n>, or tell __BOT_NAME__ \"save this rule: ...\" (owner only).\n")

def rules():
    return [l[2:].strip() for l in read(MEMORY).splitlines() if l.startswith("- ") and l[2:].strip()]

def save_rules(items):
    with open(MEMORY, "w", encoding="utf-8") as f:
        f.write(MEM_HEADER + "".join(f"- {r}\n" for r in items))
    _cache.pop(MEMORY, None)

def add_rule(rule):
    rule = " ".join(rule.split())[:300]
    items = rules()
    if rule and rule not in items:
        save_rules(items + [rule])

def del_rule(n):
    items = rules()
    if 1 <= n <= len(items):
        items.pop(n - 1)
        save_rules(items)
        return True
    return False

def _migrate_old_memory():
    # earlier versions kept MEMORY.md in workspace/ and notes in workspace/memory/ → move them into data/
    ws_mem = os.path.join(WS, "MEMORY.md")
    if os.path.exists(ws_mem) and not os.path.exists(MEMORY):
        os.replace(ws_mem, MEMORY)
    old_notes = os.path.join(WS, "memory")
    if os.path.isdir(old_notes):
        for f in os.listdir(old_notes):
            dst = os.path.join(NOTES_DIR, f)
            if not os.path.exists(dst):
                os.replace(os.path.join(old_notes, f), dst)
    old = os.path.join(BASE, "memory.md")
    if os.path.exists(old):
        lines = [l[2:].strip() for l in open(old, encoding="utf-8").read().splitlines() if l.startswith("- ")]
        for l in lines:
            add_rule(l)
        os.replace(old, old + ".migrated")
        log.info("migrated %d rules from memory.md to workspace/MEMORY.md", len(lines))
    if not os.path.exists(MEMORY):
        save_rules([])

_migrate_old_memory()

# ---------- skills ----------
_skills = {}

def skills():
    """{name: (description, path)} from workspace/skills/*/SKILL.md (frontmatter name/description)."""
    if _skills:
        return _skills
    if os.path.isdir(SKILLS_DIR):
        for d in sorted(os.listdir(SKILLS_DIR)):
            path = os.path.join(SKILLS_DIR, d, "SKILL.md")
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
            meta = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M)) if m else {}
            _skills[meta.get("name", d)] = (meta.get("description", "").strip(), path)
    return _skills

def skill_body(name):
    s = skills().get(name.strip().lower())
    if not s:
        return None
    text = open(s[1], encoding="utf-8").read()
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.S).strip()

# ---------- daily notes (audit trail, not loaded into prompts) ----------
def note(text):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with open(os.path.join(NOTES_DIR, f"{day}.md"), "a", encoding="utf-8") as f:
        f.write(f"- {time.strftime('%H:%M', time.gmtime())} UTC {text}\n")

def prune_notes(days=30):
    cutoff = time.time() - days * 86400
    for f in os.listdir(NOTES_DIR):
        p = os.path.join(NOTES_DIR, f)
        if f.endswith(".md") and os.path.getmtime(p) < cutoff:
            os.remove(p)

# ---------- language ----------
LANG_RULES = {
    "bn": "Server rule: always reply in Bangla script.",
    "banglish": "Server rule: always reply in Banglish (Bangla in English letters).",
    "en": "Server rule: always reply in English.",
}
LANGUAGE = """# Language and writing quality
Reply in the same language and script the user writes in.
- Bangla script → Bangla script only: correct standard Bangladeshi spelling (আমি, তুমি, আপনি, কেমন, ভালো, করবো,
  হয়েছে, সার্ভার). NEVER mix Hindi/Devanagari letters (like ा ो ं ध) into Bangla. Use common words, not Sanskrit-heavy ones.
- Banglish (Bangla in English letters, e.g. "kemon acho", "ki korteso") → natural casual Banglish with common
  spellings: ami, tumi, apni, kemon, bhalo/valo, korbo, hobe, ache, nai, ekhon, server, bhai. Not English, not Bangla script.
- Mixed Bangla + English is fine if the user mixes. Any other language → that language.
- Proofread before sending: no typos, no broken words, no repeated sentences."""
BANGLISH_WORDS = set("""
ami tumi apni amra tomra tora tui tomar amar apnar tor kemon acho achen achis achi asi ase ache ki kii keno kno
kothay koi kobe kivabe kibhabe valo bhalo kharap hobe hbe hoibo hoise hoyeche hocche hoy korbo korte koro korchi
korcho korteso korsi korso kore bolo bolen bol jani jano janina janish nai nei ekta khub onek bhai vai apu dada
kisu kichu kichhu kotha dekho dekhi lagbe lage parbo pari paro parsi jabo jao jasi asho ashen thik accha acha
shuno bujhi bujhlam bujhsi kintu ekhon ekhane okhane shob sob tomake amake kalke aj ajke khaiso khaise ghum
mone rakho rakhbo khelai khelo khela khelbo dao daw den deo koren korun naki jodi tahole kemne bujhai bujhao
""".split())
BN_SCRIPT = re.compile(r"[ঀ-৿]")

def lang_hint(text):
    if BN_SCRIPT.search(text):
        return "(The user is writing Bangla script.)"
    words = re.findall(r"[a-z]+", text.lower())
    hits = sum(w in BANGLISH_WORDS for w in words)
    if hits >= 2 or (hits == 1 and len(words) <= 3):
        return "(The user is writing Banglish: reply in Banglish.)"
    return ""

# ---------- prompt ----------
def build_prompt(*, gid, text, user_name, is_owner, roles, profile, facts, tool_lines, place, auto_channel,
                 ticket=False, extra_rules="", mentionable=()):
    parts = [read(f) for f in BOOT_FILES]
    parts.append(LANGUAGE)

    if tool_lines:
        parts.append("# Tools available right now\n" + "\n".join(f"- {t}" for t in tool_lines))
    sk = skills()
    if sk:
        parts.append("# Skills (load with `TOOL: skill | <name>` before multi-step work)\n"
                     + "\n".join(f"- {n}: {d}" for n, (d, _) in sk.items()))

    now = time.time()
    who = [f"# Right now",
           f"Time: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC "
           f"(Bangladesh {time.strftime('%a %H:%M', time.gmtime(now + 6 * 3600))}).",
           f"Place: {place}." + (" This is an auto-reply channel (NO_REPLY / REACT allowed)." if auto_channel else ""),
           f"Talking with: {profile.get('nickname') or user_name}"
           + (" — this is __OWNER__, your owner." if is_owner else "")
           + (f" (roles: {', '.join(roles[:6])})" if roles else "")]
    if profile.get("language"):
        who.append(f"Their preferred language: {profile['language']}.")
    if profile.get("style"):
        who.append(f"They prefer {profile['style']} answers.")
    if facts:
        who.append("Saved facts about them (use naturally, never list them):\n" + "\n".join(f"- {f}" for f in facts))
    if mentionable:
        who.append("Others in this conversation you can @mention by name: " + ", ".join(mentionable) + ".")
    parts.append("\n".join(who))

    r = rules()
    if r:
        parts.append("# Standing instructions from __OWNER__ (MEMORY.md, always follow)\n"
                     + "\n".join(f"- {x}" for x in r)[:2000])
    if extra_rules:
        parts.append(extra_rules)

    lang_rule = ""
    if gid:
        g = guild(gid)
        from servertypes import focus
        parts.append(focus(g.get("type", "general")))
        if g["knowledge"] and (ticket or g["knowledge_scope"] == "all"):
            parts.append("# This server's info from its admins (trust it for questions about this server)\n"
                         + g["knowledge"][:1500])
        lang_rule = LANG_RULES.get(g["lang"], "")
    if ticket:
        parts.append("# Ticket mode\nYou are first-line support in a support ticket. Follow skill `ticket-support` "
                     "(load it if the issue is not trivial).")
    parts.append(lang_rule or lang_hint(text))
    return "\n\n".join(p for p in parts if p)
