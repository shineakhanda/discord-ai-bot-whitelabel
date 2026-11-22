"""Headless Chrome for the bot (no extra Python packages).

- Finds Chrome/Chromium, or downloads Google's `chrome-headless-shell` on first start (Linux x86_64).
- If the host (e.g. a Pterodactyl container) lacks system libraries, it fetches just those .so files from the
  Debian/Ubuntu archive into data/chrome/lib — no root, no apt.
- Drives the browser over the DevTools protocol: renders React / Next / Vue / Svelte sites, waits until their data
  has loaded, scrolls to trigger lazy content, then reads the page text AND its components (product cards with
  name, price, old price, stock, buttons, links), can click/type for multi-step pages, and takes screenshots.
- One shared browser process, closed again after a few idle minutes to save RAM.
"""
import asyncio, base64, io, json, lzma, gzip, os, re, shutil, tarfile, time, zipfile, platform
from urllib.parse import urlparse

import aiohttp

from store import log, env, BASE

ROOT = os.path.join(BASE, "data", "chrome")
LIBDIR = os.path.join(ROOT, "lib")
CFT_JSON = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
STATE = {"path": "", "ok": None, "why": "", "installing": False}
IDLE_CLOSE = int(env("CHROME_IDLE_SECONDS", "240") or 240)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------------------------------------------
# finding / installing Chrome
# ---------------------------------------------------------------------------------------------------------------
def _local_bin():
    for root, _, files in os.walk(ROOT):
        if "chrome-headless-shell" in files and "lib" not in root.split(os.sep):
            return os.path.join(root, "chrome-headless-shell")
    return ""

def find_chrome():
    p = env("CHROME_PATH")
    if p and os.path.exists(p):
        return p
    for name in ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        w = shutil.which(name)
        if w:
            return w
    return _local_bin()

FONTDIR = os.path.join(ROOT, "fonts")

def _env():
    e = dict(os.environ)
    conf = os.path.join(FONTDIR, "fonts.conf")
    if os.path.exists(conf):
        e["FONTCONFIG_FILE"] = conf
    if os.path.isdir(LIBDIR):
        e["LD_LIBRARY_PATH"] = LIBDIR + (":" + e["LD_LIBRARY_PATH"] if e.get("LD_LIBRARY_PATH") else "")
    e.setdefault("HOME", ROOT)
    return e

async def _run(args, timeout=30):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                             env=_env())
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "", "timeout"
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

async def _missing_libs(binary):
    if not shutil.which("ldd"):
        return []
    code, out, err = await _run(["ldd", binary], 20)
    return sorted({m.group(1) for m in re.finditer(r"^\s*(\S+)\s*=>\s*not found", out + err, re.M)})

# shared library -> Debian/Ubuntu package names to try (newest name first)
LIB_PKG = {
    "libglib-2.0": ["libglib2.0-0t64", "libglib2.0-0"], "libgobject-2.0": ["libglib2.0-0t64", "libglib2.0-0"],
    "libgio-2.0": ["libglib2.0-0t64", "libglib2.0-0"], "libgmodule-2.0": ["libglib2.0-0t64", "libglib2.0-0"],
    "libnspr4": ["libnspr4"], "libplc4": ["libnspr4"], "libplds4": ["libnspr4"],
    "libnss3": ["libnss3"], "libnssutil3": ["libnss3"], "libsmime3": ["libnss3"], "libsoftokn3": ["libnss3"],
    "libfreebl3": ["libnss3"], "libfreeblpriv3": ["libnss3"],
    "libatk-1.0": ["libatk1.0-0t64", "libatk1.0-0"], "libatk-bridge-2.0": ["libatk-bridge2.0-0t64", "libatk-bridge2.0-0"],
    "libatspi": ["libatspi2.0-0t64", "libatspi2.0-0"], "libdbus-1": ["libdbus-1-3"],
    "libX11": ["libx11-6"], "libX11-xcb": ["libx11-xcb1"], "libXcomposite": ["libxcomposite1"],
    "libXdamage": ["libxdamage1"], "libXext": ["libxext6"], "libXfixes": ["libxfixes3"], "libXrandr": ["libxrandr2"],
    "libXrender": ["libxrender1"], "libXi": ["libxi6"], "libXau": ["libxau6"], "libXdmcp": ["libxdmcp6"],
    "libXtst": ["libxtst6"], "libxcb": ["libxcb1"], "libxkbcommon": ["libxkbcommon0"], "libxshmfence": ["libxshmfence1"],
    "libgbm": ["libgbm1"], "libdrm": ["libdrm2"], "libwayland-server": ["libwayland-server0"],
    "libexpat": ["libexpat1"], "libudev": ["libudev1"], "libasound": ["libasound2t64", "libasound2"],
    "libffi": ["libffi8", "libffi7"], "libpcre2-8": ["libpcre2-8-0"], "libmount": ["libmount1"],
    "libselinux": ["libselinux1"], "libsystemd": ["libsystemd0"], "libcap": ["libcap2"], "libblkid": ["libblkid1"],
    "libgcrypt": ["libgcrypt20"], "liblz4": ["liblz4-1"], "libzstd": ["libzstd1"], "libbsd": ["libbsd0"],
    "libmd": ["libmd0"], "libgpg-error": ["libgpg-error0"], "libz": ["zlib1g"], "libcups": ["libcups2t64", "libcups2"],
    "libpango-1.0": ["libpango-1.0-0"], "libcairo": ["libcairo2"], "libfontconfig": ["libfontconfig1"],
    "libfreetype": ["libfreetype6"], "libpng16": ["libpng16-16t64", "libpng16-16"], "libuuid": ["libuuid1"],
    "libbrotlidec": ["libbrotli1"], "libbrotlicommon": ["libbrotli1"], "libharfbuzz": ["libharfbuzz0b"],
    "libgraphite2": ["libgraphite2-3"], "libpixman-1": ["libpixman-1-0"], "libxml2": ["libxml2"],
}

def _pkg_for(lib):
    stem = re.sub(r"\.so.*$", "", lib)
    return LIB_PKG.get(stem) or LIB_PKG.get(re.sub(r"-[\d.]+$", "", stem)) or []

def _distro():
    info = {}
    try:
        for line in open("/etc/os-release"):
            k, _, v = line.strip().partition("=")
            info[k] = v.strip('"')
    except OSError:
        pass
    if info.get("ID") == "ubuntu" or "ubuntu" in info.get("ID_LIKE", ""):
        return "ubuntu", info.get("VERSION_CODENAME") or "noble"
    return "debian", info.get("VERSION_CODENAME") or "bookworm"

async def _packages_index(session):
    """name -> (mirror, Filename) from the distro's main (and universe) package index."""
    distro, code = _distro()
    if distro == "ubuntu":
        mirror = "http://archive.ubuntu.com/ubuntu"
        sources = [(mirror, f"dists/{code}/{c}/binary-amd64/Packages") for c in ("main", "universe")]
    else:
        mirror = "https://deb.debian.org/debian"
        sources = [(mirror, f"dists/{code}/main/binary-amd64/Packages")]
    idx = {}
    for mir, path in sources:
        raw = None
        for ext, dec in ((".xz", lzma.decompress), (".gz", gzip.decompress)):
            try:
                async with session.get(f"{mir}/{path}{ext}", timeout=aiohttp.ClientTimeout(total=180)) as r:
                    if r.status == 200:
                        raw = dec(await r.read()).decode("utf-8", "replace")
                        break
            except Exception as e:
                log.info("package index %s%s: %s", path, ext, e)
        if not raw:
            continue
        for block in raw.split("\n\n"):
            n = re.search(r"^Package: (.+)$", block, re.M)
            f = re.search(r"^Filename: (.+)$", block, re.M)
            if n and f and n.group(1) not in idx:
                idx[n.group(1)] = (mir, f.group(1))
    log.info("chrome libs: %s %s package index has %d packages", distro, code, len(idx))
    return idx

def _deb_extract_libs(data, dest, want=lambda name: ".so" in os.path.basename(name) and "x86_64-linux-gnu" in name):
    """Copy the shared libraries (or other files `want` picks) out of a .deb (ar -> data.tar.*) into dest."""
    if not data.startswith(b"!<arch>\n"):
        return []
    pos, members = 8, {}
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        name = hdr[:16].decode().strip().rstrip("/")
        size = int(hdr[48:58].decode().strip())
        members[name] = data[pos + 60:pos + 60 + size]
        pos += 60 + size + (size % 2)
    tar_name = next((n for n in members if n.startswith("data.tar")), None)
    if not tar_name:
        return []
    blob = members[tar_name]
    if tar_name.endswith(".zst"):
        log.warning("chrome libs: %s uses zstd, can't unpack it here", tar_name)
        return []
    got = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        files = {m.name: m for m in tf.getmembers()}
        for m in tf.getmembers():
            base = os.path.basename(m.name)
            if not want(m.name):
                continue
            src = m
            for _ in range(5):                            # follow symlinks inside the package
                if not src.issym():
                    break
                target = os.path.normpath(os.path.join(os.path.dirname(src.name), src.linkname))
                src = files.get(target) or files.get("./" + target.lstrip("./")) or src
                if src is m:
                    break
            if not src.isfile():
                continue
            fh = tf.extractfile(src)
            if fh:
                with open(os.path.join(dest, base), "wb") as out:
                    out.write(fh.read())
                got.append(base)
    return got

async def _install_libs(binary):
    os.makedirs(LIBDIR, exist_ok=True)
    tried, index = set(), None
    async with aiohttp.ClientSession() as s:
        for _ in range(5):                                # new libraries can need more libraries
            missing = await _missing_libs(binary)
            for f in os.listdir(LIBDIR):                  # libraries we fetched need theirs too
                if ".so" in f:
                    missing += await _missing_libs(os.path.join(LIBDIR, f))
            missing = sorted(set(missing))
            if not missing:
                return True
            pkgs = []
            for lib in missing:
                for p in _pkg_for(lib):
                    if p not in tried:
                        pkgs.append(p)
            if not pkgs:
                log.warning("chrome libs: don't know which package has %s", ", ".join(missing))
                return False
            if index is None:
                index = await _packages_index(s)
                if not index:
                    return False
            for p in dict.fromkeys(pkgs):
                tried.add(p)
                if p not in index:
                    continue
                mir, fn = index[p]
                try:
                    async with s.get(f"{mir}/{fn}", timeout=aiohttp.ClientTimeout(total=180)) as r:
                        data = await r.read()
                    got = _deb_extract_libs(data, LIBDIR)
                    log.info("chrome libs: %s -> %d files", p, len(got))
                except Exception as e:
                    log.warning("chrome libs: %s failed: %s", p, e)
    return not await _missing_libs(binary)

FONT_PKGS = ["fonts-dejavu-core", "fonts-beng-extra", "fonts-noto-color-emoji"]

def _have_fonts():
    for d in ("/usr/share/fonts", "/usr/local/share/fonts", FONTDIR):
        for _, _, files in os.walk(d):
            if any(f.endswith((".ttf", ".otf", ".ttc")) for f in files):
                return True
    return False

async def _install_fonts():
    """Screenshots need fonts (Latin + Bangla + emoji). Fetch a few small font packages if the host has none."""
    if _have_fonts():
        return
    os.makedirs(FONTDIR, exist_ok=True)
    try:
        async with aiohttp.ClientSession() as s:
            index = await _packages_index(s)
            for p in FONT_PKGS:
                if p not in index:
                    continue
                mir, fn = index[p]
                async with s.get(f"{mir}/{fn}", timeout=aiohttp.ClientTimeout(total=180)) as r:
                    data = await r.read()
                got = _deb_extract_libs(data, FONTDIR, lambda n: n.endswith((".ttf", ".otf", ".ttc")))
                log.info("chrome fonts: %s -> %d files", p, len(got))
    except Exception as e:
        log.info("font download failed: %s", e)
    with open(os.path.join(FONTDIR, "fonts.conf"), "w") as f:
        f.write(f"""<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>
<dir>{FONTDIR}</dir><dir>/usr/share/fonts</dir><dir>/usr/local/share/fonts</dir>
<cachedir>{os.path.join(ROOT, "fontcache")}</cachedir></fontconfig>""")

async def _download_shell():
    async with aiohttp.ClientSession() as s:
        async with s.get(CFT_JSON, timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json(content_type=None)
        dl = data["channels"]["Stable"]["downloads"]["chrome-headless-shell"]
        url = next(d["url"] for d in dl if d["platform"] == "linux64")
        os.makedirs(ROOT, exist_ok=True)
        zpath = os.path.join(ROOT, "shell.zip")
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=600)) as r:
            if r.status != 200:
                raise RuntimeError(f"download returned {r.status}")
            with open(zpath, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 16):
                    f.write(chunk)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            z.extract(info, ROOT)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(os.path.join(ROOT, info.filename), mode)
    os.remove(zpath)
    p = _local_bin()
    if p:
        os.chmod(p, 0o755)
    return p

async def ensure_chrome():
    """Called once at startup (in the background). Sets STATE['ok']."""
    if STATE["installing"] or STATE["ok"]:
        return STATE["ok"]
    STATE["installing"] = True
    try:
        if env("CHROME", "1") == "0":
            STATE.update(ok=False, why="turned off (CHROME=0)")
            return False
        p = find_chrome()
        if not p and env("CHROME_AUTO_INSTALL", "1") != "0":
            if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
                STATE.update(ok=False, why="auto-install needs Linux x86_64")
                return False
            log.info("downloading headless Chrome (first start only)…")
            p = await _download_shell()
        if not p:
            STATE.update(ok=False, why="no Chrome found")
            return False
        STATE["path"] = p
        missing = await _missing_libs(p)
        if missing:
            log.info("Chrome needs %d system libraries; fetching them…", len(missing))
            if not await _install_libs(p):
                left = await _missing_libs(p)
                STATE.update(ok=False, why="missing system libraries: " + ", ".join(left[:6]))
                log.warning("headless Chrome unavailable: %s", STATE["why"])
                return False
        await _install_fonts()
        try:                                              # real launch test
            async with Browser.lock:
                await Browser.start()
            STATE.update(ok=True, why="")
            log.info("headless Chrome ready (%s)", os.path.basename(p))
        except Exception as e:
            STATE.update(ok=False, why=f"launch failed: {str(e)[:160]}")
            log.warning("headless Chrome unavailable: %s", STATE["why"])
        return STATE["ok"]
    except Exception as e:
        STATE.update(ok=False, why=str(e)[:160])
        log.warning("headless Chrome setup failed: %s", e)
        return False
    finally:
        STATE["installing"] = False

def ready():
    return bool(STATE["ok"])

# ---------------------------------------------------------------------------------------------------------------
# DevTools protocol client
# ---------------------------------------------------------------------------------------------------------------
class Browser:
    proc = None
    ws = None
    session = None
    reader = None
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(int(env("BROWSER_CONCURRENCY", "2") or 2))
    next_id = 0
    pending = {}
    listeners = {}            # sessionId -> callback(method, params)
    last_used = 0.0
    closer = None

    @classmethod
    async def start(cls):
        if cls.proc and cls.proc.returncode is None and cls.ws is not None and not cls.ws.closed:
            return
        await cls.stop()
        prof = os.path.join(ROOT, "profile")
        shutil.rmtree(prof, ignore_errors=True)
        os.makedirs(prof, exist_ok=True)
        args = [STATE["path"] or find_chrome(), "--headless=new", "--remote-debugging-port=0", "--no-sandbox",
                "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking", "--disable-sync", "--mute-audio",
                "--hide-scrollbars", "--disable-features=Translate,MediaRouter,OptimizationHints",
                "--js-flags=--max-old-space-size=256", "--renderer-process-limit=2",
                f"--user-data-dir={prof}", "--window-size=1366,900", f"--user-agent={UA}", "about:blank"]
        cls.proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE, env=_env())
        ws_url, tail = None, []
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(cls.proc.stderr.readline(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            tail.append(s)
            m = re.search(r"DevTools listening on (ws://\S+)", s)
            if m:
                ws_url = m.group(1)
                break
        if not ws_url:
            await cls.stop()
            raise RuntimeError(" / ".join(tail[-3:])[:300] or "Chrome did not start")
        asyncio.create_task(cls._drain(cls.proc))
        cls.session = aiohttp.ClientSession()
        cls.ws = await cls.session.ws_connect(ws_url, max_msg_size=64 << 20, heartbeat=30)
        cls.reader = asyncio.create_task(cls._read())
        if cls.closer is None or cls.closer.done():
            cls.closer = asyncio.create_task(cls._idle_closer())

    @staticmethod
    async def _drain(proc):
        try:
            while proc.stderr and await proc.stderr.readline():
                pass
        except Exception:
            pass

    @classmethod
    async def stop(cls):
        if cls.reader:
            cls.reader.cancel()
        for fut in cls.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("browser closed"))
        cls.pending.clear()
        cls.listeners.clear()
        try:
            if cls.ws is not None:
                await cls.ws.close()
            if cls.session is not None:
                await cls.session.close()
        except Exception:
            pass
        if cls.proc and cls.proc.returncode is None:
            try:
                cls.proc.kill()
                await cls.proc.wait()
            except Exception:
                pass
        cls.proc = cls.ws = cls.session = cls.reader = None

    @classmethod
    async def _idle_closer(cls):
        while True:
            await asyncio.sleep(30)
            if cls.proc and not cls.listeners and time.time() - cls.last_used > IDLE_CLOSE:
                async with cls.lock:
                    if not cls.listeners and time.time() - cls.last_used > IDLE_CLOSE:
                        log.info("closing idle headless Chrome")
                        await cls.stop()

    @classmethod
    async def _read(cls):
        try:
            async for msg in cls.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data:
                    fut = cls.pending.pop(data["id"], None)
                    if fut and not fut.done():
                        if "error" in data:
                            fut.set_exception(RuntimeError(data["error"].get("message", "CDP error")))
                        else:
                            fut.set_result(data.get("result", {}))
                elif "method" in data:
                    cb = cls.listeners.get(data.get("sessionId"))
                    if cb:
                        try:
                            cb(data["method"], data.get("params", {}))
                        except Exception as e:
                            log.debug("cdp listener: %s", e)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.info("browser connection ended: %s", e)

    @classmethod
    async def send(cls, method, params=None, sid=None, timeout=30):
        cls.next_id += 1
        mid = cls.next_id
        fut = asyncio.get_running_loop().create_future()
        cls.pending[mid] = fut
        msg = {"id": mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        await cls.ws.send_str(json.dumps(msg))
        return await asyncio.wait_for(fut, timeout)


class Page:
    """One tab. Use `async with Page() as p:`."""
    def __init__(self):
        self.sid = self.tid = None
        self.inflight = set()
        self.last_net = time.monotonic()
        self.loaded = asyncio.Event()
        self.blocked = []

    async def __aenter__(self):
        await Browser.sem.acquire()
        try:
            async with Browser.lock:
                await Browser.start()
            Browser.last_used = time.time()
            t = await Browser.send("Target.createTarget", {"url": "about:blank"})
            self.tid = t["targetId"]
            a = await Browser.send("Target.attachToTarget", {"targetId": self.tid, "flatten": True})
            self.sid = a["sessionId"]
            Browser.listeners[self.sid] = self._on
            for m, p in (("Page.enable", {}), ("Network.enable", {}), ("Runtime.enable", {}),
                         ("Network.setUserAgentOverride", {"userAgent": UA, "acceptLanguage": "en-US,en;q=0.9"}),
                         ("Emulation.setDeviceMetricsOverride", {"width": 1366, "height": 900,
                                                                  "deviceScaleFactor": 1, "mobile": False}),
                         ("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document"}]})):
                await self.send(m, p)
        except Exception:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc):
        try:
            if self.sid:
                Browser.listeners.pop(self.sid, None)
            if self.tid and Browser.ws is not None:
                await Browser.send("Target.closeTarget", {"targetId": self.tid}, timeout=10)
        except Exception:
            pass
        Browser.last_used = time.time()
        Browser.sem.release()

    def send(self, method, params=None, timeout=30):
        return Browser.send(method, params, self.sid, timeout)

    def _on(self, method, p):
        if method == "Network.requestWillBeSent":
            self.inflight.add(p.get("requestId"))
            self.last_net = time.monotonic()
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            self.inflight.discard(p.get("requestId"))
            self.last_net = time.monotonic()
        elif method == "Page.loadEventFired":
            self.loaded.set()
        elif method == "Fetch.requestPaused":
            asyncio.create_task(self._guard(p))

    async def _guard(self, p):
        """Every page/iframe the site navigates to must be public (no localhost / private network)."""
        import web
        rid, url = p.get("requestId"), (p.get("request") or {}).get("url", "")
        ok = True
        if url.startswith(("http://", "https://")):
            try:
                await web.check_url(url)
            except Exception:
                ok = False
        elif not url.startswith(("about:", "data:", "blob:")):
            ok = False
        try:
            if ok:
                await self.send("Fetch.continueRequest", {"requestId": rid})
            else:
                self.blocked.append(url[:100])
                await self.send("Fetch.failRequest", {"requestId": rid, "errorReason": "AccessDenied"})
        except Exception:
            pass

    async def idle(self, quiet=0.8, cap=12.0):
        """Wait until the network has been quiet for `quiet` seconds (long-polling connections allowed)."""
        end = time.monotonic() + cap
        while time.monotonic() < end:
            if len(self.inflight) <= 1 and time.monotonic() - self.last_net >= quiet:
                return
            await asyncio.sleep(0.2)

    async def goto(self, url, wait=25.0):
        self.loaded.clear()
        r = await self.send("Page.navigate", {"url": url}, timeout=wait + 5)
        if r.get("errorText"):
            if self.blocked:
                raise RuntimeError("that address points to a private network")
            raise RuntimeError(f"couldn't open the page ({r['errorText']})")
        try:
            await asyncio.wait_for(self.loaded.wait(), wait)
        except asyncio.TimeoutError:
            pass
        await self.idle(cap=10)

    async def js(self, expr, timeout=20):
        r = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True},
                            timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError((r["exceptionDetails"].get("exception") or {}).get("description", "script error")[:200])
        return (r.get("result") or {}).get("value")

    async def scroll_all(self, steps=8):
        await self.js("""(async () => { const h = () => document.documentElement.scrollHeight;
            for (let i = 0; i < %d; i++) { window.scrollBy(0, innerHeight * 0.9);
              await new Promise(r => setTimeout(r, 350)); if (innerHeight + scrollY >= h() - 5) break; }
            window.scrollTo(0, 0); })()""" % steps, timeout=15)
        await self.idle(cap=5)

    async def click_text(self, text):
        box = await self.js("""(() => { const want = %s.toLowerCase().trim();
            const els = [...document.querySelectorAll('a,button,[role=button],[role=tab],[role=link],[role=option],'
              + 'li,summary,label,[onclick],[class*=tab],[class*=categ],[class*=filter],[class*=btn],span,div')];
            const vis = e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
            const t = e => (e.innerText || e.getAttribute('aria-label') || '').trim().toLowerCase();
            let el = els.find(e => vis(e) && t(e) === want) || els.filter(e => vis(e) && t(e).includes(want))
                       .sort((a, b) => t(a).length - t(b).length)[0];
            if (!el) return null; el.scrollIntoView({block: 'center'}); const r = el.getBoundingClientRect();
            return {x: r.x + r.width / 2, y: r.y + r.height / 2, label: t(el).slice(0, 60)}; })()""" % json.dumps(text))
        if not box:
            return None
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", {"type": kind, "x": box["x"], "y": box["y"],
                                                          "button": "left", "clickCount": 1})
        await asyncio.sleep(0.4)
        await self.idle(cap=8)
        return box["label"]

    async def type_into(self, field, text):
        ok = await self.js("""(() => { const want = %s.toLowerCase();
            const ins = [...document.querySelectorAll('input:not([type=hidden]):not([type=password]),textarea,[contenteditable=true]')];
            const el = ins.find(e => ((e.placeholder || '') + ' ' + (e.name || '') + ' ' + (e.ariaLabel || '') + ' '
                         + (e.type || '')).toLowerCase().includes(want)) || ins[0];
            if (!el) return false; el.scrollIntoView({block: 'center'}); el.focus();
            if ('value' in el) el.value = ''; return true; })()""" % json.dumps(field or "search"))
        if not ok:
            return False
        await self.send("Input.insertText", {"text": text})
        await self.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                                    "windowsVirtualKeyCode": 13})
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                                    "windowsVirtualKeyCode": 13})
        await asyncio.sleep(0.5)
        await self.idle(cap=8)
        return True

    async def screenshot(self, full=False, fmt="png", quality=80):
        params = {"format": fmt}
        if fmt == "jpeg":
            params["quality"] = quality
        if full:
            h = await self.js("Math.min(document.documentElement.scrollHeight, 4000)") or 900
            params.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": 1366, "height": h, "scale": 1})
        r = await self.send("Page.captureScreenshot", params, timeout=40)
        return base64.b64decode(r["data"])

    async def extract(self):
        return await self.js(EXTRACT_JS, timeout=25)


# ---------------------------------------------------------------------------------------------------------------
# what we read from a page: text + structured components
# ---------------------------------------------------------------------------------------------------------------
EXTRACT_JS = r"""(() => {
const PRICE = /(?:[৳$€£₹]|\b(?:tk|bdt|usd|inr|eur|rs\.?|taka)\b)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:[৳$€£₹]|\b(?:tk|bdt|taka|usd|inr|eur)\b|\/\s?(?:mo|month|yr|year))/i;
const STOCK = /(?:^|[^a-z])(in[\s-]?stock(?:\s*[:\-]?\s*\d+)?|out[\s-]of[\s-]stock|sold[\s-]?out|stock\s*[:\-]?\s*\d+|(?<![\d.,৳$€£₹])\d+\s+(?:in stock|left|available|pcs|units|items|remaining|keys|accounts)|unavailable|available|pre-?order|coming soon|limited stock|restocking|instant delivery)(?![a-z])/i;
const vis = e => { try { const s = getComputedStyle(e); if (s.display === 'none' || s.visibility === 'hidden') return false; } catch (x) {} return true; };
const txt = e => { if (!e) return ''; const out = []; const w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const p = w.currentNode.parentElement; if (p && /^(SCRIPT|STYLE|NOSCRIPT)$/.test(p.tagName)) continue; out.push(w.currentNode.textContent); }
  return out.join(' ').replace(/\s+/g, ' ').trim(); };
const sig = e => e.tagName + '.' + (typeof e.className === 'string' ? e.className.split(/\s+/).slice(0, 3).sort().join('.') : '');
const struck = e => { for (let n = e; n && n !== document.body; n = n.parentElement) {
  if (/^(DEL|S|STRIKE)$/.test(n.tagName)) return true;
  try { if (getComputedStyle(n).textDecorationLine.includes('line-through')) return true; } catch (x) {}
  if (/old|compare|was|regular|strike|original/i.test(typeof n.className === 'string' ? n.className : '')) return true; }
  return false; };
// 1) leaf elements that hold a price
const leaves = [...document.querySelectorAll('body *')].filter(e => {
  if (!vis(e) || /^(SCRIPT|STYLE|NOSCRIPT|SVG|PATH)$/.test(e.tagName)) return false;
  const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
  return PRICE.test(own) || (e.children.length === 0 && PRICE.test(txt(e)));
});
// 2) climb to the repeated "card" around each price
const cards = new Set();
for (const leaf of leaves) {
  let card = null, n = leaf;
  for (let d = 0; d < 8 && n && n.parentElement && n !== document.body; d++, n = n.parentElement) {
    const p = n.parentElement, s = sig(n);
    const twins = [...p.children].filter(c => c !== n && sig(c) === s && PRICE.test(txt(c)));
    if (twins.length >= 1 && txt(n).length < 1500) { card = n; break; }
  }
  if (!card) { n = leaf; for (let d = 0; d < 5 && n.parentElement && txt(n.parentElement).length < 500; d++) n = n.parentElement; card = n; }
  cards.add(card);
}
// drop cards that contain other cards
const list = [...cards].filter(c => ![...cards].some(o => o !== c && c.contains(o)));
const items = [];
for (const c of list.slice(0, 150)) {
  const t = txt(c);
  const nameEl = c.querySelector('h1,h2,h3,h4,h5,h6,[class*=title],[class*=name],[class*=Title],[class*=Name]') ;
  let name = txt(nameEl);
  if (!name || PRICE.test(name) && name.length < 12) {
    name = (t.split(/(?<=\S)\s{0,}(?=[৳$€£₹])/)[0] || t).slice(0, 80);
  }
  const prices = [], old = [];
  for (const e of c.querySelectorAll('*')) {
    const own = [...e.childNodes].filter(x => x.nodeType === 3).map(x => x.textContent).join(' ').trim();
    const m = own.match(PRICE); if (!m) continue;
    (struck(e) ? old : prices).push(m[0].trim());
  }
  if (!prices.length) { const m = t.match(PRICE); if (m) prices.push(m[0]); }
  let stock = ((t.match(STOCK) || ['', ''])[1] || '').trim();
  const btns = [...c.querySelectorAll('button,a[class*=btn],a[class*=button],[role=button]')].map(b => ({t: txt(b).slice(0, 30), off: b.disabled || b.getAttribute('aria-disabled') === 'true' || /disabled/i.test(b.className || '')})).filter(b => b.t);
  if (!stock && btns.some(b => b.off)) stock = 'unavailable (button disabled)';
  const badges = [...c.querySelectorAll('[class*=badge],[class*=tag],[class*=label],[class*=chip],[class*=pill],[class*=ribbon]')].map(txt).filter(x => x && x.length < 25 && !PRICE.test(x)).slice(0, 4);
  const a = c.tagName === 'A' ? c : c.querySelector('a[href]');
  const img = c.querySelector('img');
  items.push({name: name.slice(0, 100), price: [...new Set(prices)].slice(0, 3).join(' / '), old: [...new Set(old)].slice(0, 2).join(' / '),
    stock: stock, badges: [...new Set(badges)].join(', '), buttons: [...new Set(btns.map(b => b.t + (b.off ? ' (disabled)' : '')))].slice(0, 3).join(', '),
    link: a ? a.href : '', image: img ? (img.alt || '') : '', text: t.slice(0, 220)});
}
const metas = {};
for (const m of document.querySelectorAll('meta[property^="og:"],meta[name=description],meta[property^="product:"]'))
  metas[m.getAttribute('property') || m.getAttribute('name')] = (m.content || '').slice(0, 200);
const nav = [...document.querySelectorAll('nav a, header a, [role=tab], [class*=categ] a, [class*=categ] button')].map(txt).filter(x => x && x.length < 30);
return {url: location.href, title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 80000),
  items: items, meta: metas, nav: [...new Set(nav)].slice(0, 40),
  headings: [...document.querySelectorAll('h1,h2,h3')].map(txt).filter(Boolean).slice(0, 40),
  links: [...new Set([...document.querySelectorAll('a[href]')].map(a => a.href).filter(h => /^https?:/.test(h)))].slice(0, 400)};
})()"""


def format_items(items, limit=80):
    """Product/plan cards as compact lines for the AI (and for humans)."""
    lines = []
    for it in (items or [])[:limit]:
        bits = [f"**{it.get('name') or '?'}**"]
        if it.get("price"):
            bits.append(f"price {it['price']}")
        if it.get("old"):
            bits.append(f"was {it['old']}")
        if it.get("stock"):
            bits.append(f"stock: {it['stock']}")
        if it.get("badges") and it["badges"].lower() != (it.get("stock") or "").lower():
            bits.append(f"[{it['badges']}]")
        if it.get("buttons"):
            bits.append(f"buttons: {it['buttons']}")
        if it.get("link"):
            bits.append(it["link"][:120])
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def _actions(script):
    """'click Minecraft; wait 2; type search=cape; scroll' -> [(verb, arg)]"""
    out = []
    for part in re.split(r"[;\n]+", script or ""):
        part = part.strip()
        if not part:
            continue
        verb, _, arg = part.partition(" ")
        verb = verb.lower().rstrip(":")
        if verb in ("click", "press", "open", "tap", "select"):
            out.append(("click", arg.strip().strip("'\"")))
        elif verb in ("type", "search", "fill", "enter"):
            field, _, val = arg.partition("=")
            out.append(("type", (field.strip() or "search", val.strip()) if val else ("search", field.strip())))
        elif verb in ("wait", "sleep"):
            try:
                out.append(("wait", min(10.0, float(re.sub(r"[^\d.]", "", arg) or 2))))
            except ValueError:
                out.append(("wait", 2.0))
        elif verb in ("scroll", "more"):
            out.append(("scroll", None))
        elif verb in ("goto", "visit", "navigate"):
            out.append(("goto", arg.strip()))
    return out[:12]

# never click things that spend money, sign in, submit or delete
DANGER = re.compile(r"\b(buy|purchase|checkout|check out|pay|place order|order now|add to cart|subscribe|sign ?in|log ?in|"
                    r"sign ?up|register|submit|confirm|delete|remove|logout|log out|donate|kinun|কিনুন)\b", re.I)


async def render(url, actions="", shot=False, full=False):
    """Open url in headless Chrome, run optional actions, return dict(url,title,text,items,meta,nav,headings,png,log)."""
    if not ready():
        raise RuntimeError("headless Chrome isn't available here" + (f" ({STATE['why']})" if STATE["why"] else ""))
    done = []
    async with Page() as p:
        await p.goto(url)
        await p.scroll_all()
        for verb, arg in _actions(actions):
            if verb == "click":
                if DANGER.search(arg):
                    done.append(f"skipped clicking '{arg}' (I never click buy/login/submit buttons)")
                    continue
                lab = await p.click_text(arg)
                done.append(f"clicked '{lab}'" if lab else f"no '{arg}' found to click")
            elif verb == "type":
                field, val = arg
                ok = await p.type_into(field, val)
                done.append(f"typed '{val}' into {field}" if ok else "no text box found")
            elif verb == "wait":
                await asyncio.sleep(arg)
                await p.idle(cap=5)
                done.append(f"waited {arg:g}s")
            elif verb == "scroll":
                await p.scroll_all(12)
                done.append("scrolled the page")
            elif verb == "goto":
                await p.goto(arg)
                done.append(f"opened {arg[:60]}")
        data = await p.extract() or {}
        if shot:
            data["png"] = await p.screenshot(full=full)
    data["log"] = done
    data["text"] = re.sub(r"\n{3,}", "\n\n", data.get("text") or "")
    return data


async def page_shot(url, full=False):
    data = await render(url, shot=True, full=full)
    return data.get("url") or url, data["png"]


def as_text(data, item_limit=80):
    """Rendered page -> text for the AI: components first (so trimming keeps them), then the page text."""
    parts = []
    if data.get("items"):
        parts.append("[products / plans on this page — name · price · old price · stock · buttons · link]\n"
                     + format_items(data["items"], item_limit))
    if data.get("nav"):
        parts.append("[menu / categories] " + " · ".join(data["nav"][:30]))
    if data.get("log"):
        parts.append("[browser actions] " + "; ".join(data["log"]))
    parts.append(data.get("text") or "")
    return "\n\n".join(p for p in parts if p.strip())
