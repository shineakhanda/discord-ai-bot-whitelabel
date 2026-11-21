"""Start a white-label bot: render template/ with brand.json into app/, then run it.
Made by Shine Akhanda · shineakhanda.dev
"""
import os, re, sys, json, shutil, hashlib

ROOT = os.path.dirname(os.path.abspath(__file__))
TPL, APP = os.path.join(ROOT, "template"), os.path.join(ROOT, "app")
WATERMARK = "Made by Shine Akhanda · shineakhanda.dev"
TEXT_EXT = (".py", ".md", ".txt")

def load_env(path):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def brand():
    b = json.load(open(os.path.join(ROOT, "brand.json"), encoding="utf-8"))
    name = (b.get("name") or "Nova").strip()
    slug = re.sub(r"[^a-z0-9]+", "", name.lower()) or "bot"
    trig = [re.escape(t.strip().lower()) for t in (b.get("triggers") or [name]) if t.strip()] or [re.escape(slug)]
    color = (b.get("color") or "#14B8A6").lstrip("#")
    owner = (b.get("owner") or "the owner").strip()
    return {
        "__BOT_NAME__": name, "__BOT_UPPER__": name.upper(), "__bot_slug__": slug,
        "__TRIGGERS__": "|".join(trig), "__OWNER__": owner,
        "__OWNER_NOTES__": (b.get("owner_notes") or "").strip(),
        "__VIBE__": b.get("vibe") or "friendly, smart, chill, reliable",
        "__STYLE__": b.get("style") or "Friendly and warm, quick, a little funny, never fake.",
        "__COLOR__": "0x" + (color if re.fullmatch(r"[0-9a-fA-F]{6}", color) else "14B8A6"),
        "__WEBSITE__": b.get("website") or "shineakhanda.dev",
        "__STREAM_URL__": b.get("stream_url") or "https://www.twitch.tv/discord",
    }

def render():
    core = open(os.path.join(TPL, "core.py"), encoding="utf-8").read()
    if WATERMARK not in core:
        sys.exit("template/core.py was changed: the 'Made by Shine Akhanda' watermark is required by the licence.")
    values = brand()
    for root, dirs, files in os.walk(TPL):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, TPL)
        for f in files:
            src, dst = os.path.join(root, f), os.path.join(APP, rel, f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if f.endswith(TEXT_EXT):
                text = open(src, encoding="utf-8").read()
                for k, v in values.items():
                    text = text.replace(k, v)
                old = open(dst, encoding="utf-8").read() if os.path.exists(dst) else None
                if old != text:
                    open(dst, "w", encoding="utf-8").write(text)
            elif not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
                shutil.copy2(src, dst)
    return values

def ensure_packages():
    """Fresh server / PC: install the bot's Python packages once."""
    try:
        import discord, aiohttp  # noqa: F401
        return
    except ImportError:
        pass
    import subprocess
    req = os.path.join(ROOT, "requirements.txt")
    if not os.path.exists(req):
        req = os.path.join(TPL, "requirements.txt")
    print("[start] installing Python packages (first start only)...", flush=True)
    cmd = [sys.executable, "-m", "pip", "install", "-U", "-r", req]
    if os.path.isdir("/home/container"):            # Pterodactyl / Pelican / Reviactyl
        cmd[4:4] = ["--prefix", ".local"]
    subprocess.run(cmd, stdin=subprocess.DEVNULL)
    os.execv(sys.executable, [sys.executable] + sys.argv)  # restart so the new packages load

if __name__ == "__main__":
    ensure_packages()
    load_env(os.path.join(ROOT, ".env"))
    os.environ.setdefault("DATA_DIR", os.path.join(ROOT, "data"))
    _b = json.load(open(os.path.join(ROOT, "brand.json"), encoding="utf-8"))
    os.environ.setdefault("BOT_FOCUS", str(_b.get("focus") or "support"))   # support | store | hosting | all
    if _b.get("job"):
        os.environ.setdefault("BOT_JOB", str(_b["job"]))
    v = render()
    print(f"[{v['__BOT_NAME__']}] rendered from brand.json · {WATERMARK}", flush=True)
    os.chdir(APP)
    os.execv(sys.executable, [sys.executable, os.path.join(APP, "bot.py")])
