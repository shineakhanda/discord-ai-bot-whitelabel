"""Web: search engines, page reading (Lightpanda headless browser or plain HTTP), and SSRF protection."""
import os, re, time, shutil, socket, asyncio, ipaddress
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin

import aiohttp

from store import cfg, env, HTTP, BASE, log

# ---------- keys ----------
TAVILY_KEY = env("TAVILY_API_KEY")
GOOGLE_KEY = env("GOOGLE_API_KEY")
GOOGLE_CX = env("GOOGLE_CSE_ID")
SERPER_KEY = env("SERPER_API_KEY")
SEARCH_COUNTRY = env("SEARCH_COUNTRY").lower()
try:
    from ddgs import DDGS
except Exception:
    DDGS = None

UA = "Mozilla/5.0 (compatible; __BOT_NAME__Bot/1.0; +https://discord.com)"
ALLOW_PRIVATE = {h.strip().lower() for h in env("ALLOW_PRIVATE_HOSTS").split(",") if h.strip()}
PAGE_CHARS = 3500          # page text sent to the AI (~900 tokens)
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)

# ---------- SSRF guard ----------
class BlockedURL(Exception):
    pass

def _ip_ok(ip):
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast

async def check_url(url, ports=None):
    """Raise BlockedURL unless url is http(s) to a public address."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BlockedURL("only http/https links are allowed")
    if p.username or p.password:
        raise BlockedURL("links with logins are not allowed")
    port = p.port or (443 if p.scheme == "https" else 80)
    if ports and port not in ports:
        raise BlockedURL(f"port {port} is not allowed")
    host = p.hostname.lower()
    if host in ALLOW_PRIVATE:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise BlockedURL("couldn't find that website")
    if not infos or not all(_ip_ok(i[4][0]) for i in infos):
        raise BlockedURL("private or local addresses are not allowed")

async def safe_get(url, max_bytes=1_500_000, ports=(80, 443)):
    """GET with manual redirects, each hop checked. Returns (final_url, content_type, text)."""
    for _ in range(5):
        await check_url(url, ports)
        async with HTTP["s"].get(url, allow_redirects=False, headers={"User-Agent": UA},
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status >= 400:
                raise RuntimeError(f"site returned {r.status}")
            ctype = r.headers.get("Content-Type", "").lower()
            body = await r.content.read(max_bytes)
            return url, ctype, body.decode(r.charset or "utf-8", errors="replace")
    raise RuntimeError("too many redirects")

async def final_url(url):
    """Follow redirects safely (HEAD, then GET fallback) to get the final public URL."""
    for _ in range(5):
        await check_url(url, (80, 443))
        try:
            async with HTTP["s"].head(url, allow_redirects=False, headers={"User-Agent": UA},
                                      timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                return url
        except aiohttp.ClientError:
            return url
    raise RuntimeError("too many redirects")

# ---------- HTML -> text (no extra packages) ----------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("## ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)

def html_to_text(html):
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.title.strip(), squeeze("".join(p.out))

def squeeze(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)            # markdown images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)         # markdown links -> text
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()

# ---------- Lightpanda headless browser ----------
LP_LOCAL = os.path.join(BASE, "lightpanda")
LP = {"path": env("LIGHTPANDA_PATH") or shutil.which("lightpanda") or (LP_LOCAL if os.path.exists(LP_LOCAL) else "")}
LP_URLS = [
    "https://github.com/lightpanda-io/browser/releases/latest/download/lightpanda-x86_64-linux",
    "https://github.com/lightpanda-io/browser/releases/download/nightly/lightpanda-x86_64-linux",
]

async def ensure_lightpanda():
    """Download the Lightpanda binary next to the bot on first start (Linux x86_64)."""
    if LP["path"] or env("LIGHTPANDA_AUTO_INSTALL", "1") == "0":
        return
    import platform
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        log.info("Lightpanda auto-install skipped (needs Linux x86_64); using HTTP reader")
        return
    for url in LP_URLS:
        try:
            async with HTTP["s"].get(url, timeout=aiohttp.ClientTimeout(total=180)) as r:
                if r.status != 200:
                    continue
                tmp = LP_LOCAL + ".part"
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
            os.chmod(tmp, 0o755)
            os.replace(tmp, LP_LOCAL)
            LP["path"] = LP_LOCAL
            code, out, err = await _run_lp(["version"], 15)
            log.info("Lightpanda installed (%s)", (out or err).strip()[:60])
            return
        except Exception as e:
            log.warning("Lightpanda download failed from %s: %s", url, e)
            LP["path"] = ""
    log.info("Lightpanda not available; using HTTP reader")
LP_FLAGS = env("LIGHTPANDA_FLAGS", "--strip-mode full").split()
_lp_sem = asyncio.Semaphore(int(env("BROWSER_CONCURRENCY", "2") or 2))   # RAM guard

async def _run_lp(args, timeout):
    proc = await asyncio.create_subprocess_exec(
        LP["path"], *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("page took too long")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

async def _lightpanda(url):
    async with _lp_sem:
        code, out, err = await _run_lp(["fetch", "--dump", "markdown", *LP_FLAGS, url], 30)
        if code != 0 and LP_FLAGS:                       # older/newer builds: retry without extra flags
            code, out, err = await _run_lp(["fetch", "--dump", "markdown", url], 30)
        if code != 0 or not out.strip():
            raise RuntimeError(f"browser failed: {err.strip()[-150:]}")
        return out

def browser_name():
    return "Lightpanda" if LP["path"] else "HTTP reader"

_page_cache = {}

async def read_page(url):
    """Returns (title, text) of a public web page, trimmed for the AI."""
    url = url.rstrip(").,!?>")
    hit = _page_cache.get(url)
    if hit and time.time() - hit[0] < 900:
        return hit[1], hit[2]
    title, text = "", ""
    if LP["path"]:
        try:
            md = await _lightpanda(await final_url(url))
            text = squeeze(md)
            m = re.search(r"^#\s+(.+)$", md, re.M)
            title = m.group(1).strip() if m else ""
        except BlockedURL:
            raise
        except Exception as e:
            log.warning("lightpanda failed for %s: %s", url, e)
    if not text:
        _, ctype, body = await safe_get(url)
        if "html" in ctype or "<html" in body[:500].lower():
            title, text = html_to_text(body)
        elif ctype.startswith("text/") or "json" in ctype:
            text = squeeze(body)
        else:
            raise RuntimeError("that link isn't a readable web page")
    if not text:
        raise RuntimeError("the page had no readable text")
    text = text[:PAGE_CHARS]
    _page_cache[url] = (time.time(), title, text)
    if len(_page_cache) > 100:
        _page_cache.pop(next(iter(_page_cache)))
    return title, text

class _Tables(HTMLParser):
    """Pull <table>s out of HTML as markdown, for pages read without the browser."""
    def __init__(self):
        super().__init__()
        self.tables, self.rows, self.row, self.cell, self.depth = [], [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            self.rows = []
        elif tag == "tr" and self.depth:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()).replace("|", "/"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            self.depth -= 1
            if self.rows:
                w = max(len(r) for r in self.rows)
                lines = ["| " + " | ".join(r + [""] * (w - len(r))) + " |" for r in self.rows]
                lines.insert(1, "|" + " --- |" * w)
                self.tables.append("\n".join(lines))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

async def read_raw(url, limit=60000):
    """Full page content (JavaScript run by Lightpanda when available) as markdown-ish text with tables kept.
    Returns (final_url, title, text)."""
    url = await final_url(url.rstrip(").,!?>"))
    if LP["path"]:
        try:
            md = await _lightpanda(url)
            m = re.search(r"^#\s+(.+)$", md, re.M)
            return url, (m.group(1).strip() if m else ""), md[:limit]
        except BlockedURL:
            raise
        except Exception as e:
            log.warning("lightpanda raw failed for %s: %s", url, e)
    _, ctype, body = await safe_get(url, max_bytes=3_000_000)
    if "html" in ctype or "<html" in body[:500].lower():
        title, text = html_to_text(body)
        t = _Tables()
        try:
            t.feed(body)
        except Exception:
            pass
        return url, title, ("\n\n".join(t.tables) + "\n\n" + text)[:limit]
    return url, "", body[:limit]

# ---------- web search ----------
def engine_ok(name):
    return {"google": bool(GOOGLE_KEY and GOOGLE_CX), "serper": bool(SERPER_KEY),
            "tavily": bool(TAVILY_KEY), "ddg": DDGS is not None}.get(name, False)

def active_engines():
    return [e for e in cfg["engines"] if engine_ok(e)]

def search_ready():
    return cfg["web"] != "off" and bool(active_engines())

async def _google(q):
    params = {"key": GOOGLE_KEY, "cx": GOOGLE_CX, "q": q, "num": 3}
    if SEARCH_COUNTRY:
        params["gl"] = SEARCH_COUNTRY
    async with HTTP["s"].get("https://www.googleapis.com/customsearch/v1", params=params) as r:
        if r.status != 200:
            raise RuntimeError(f"google {r.status}: {(await r.text())[:150]}")
        data = await r.json()
    return [(x.get("title", ""), x.get("link", ""), x.get("snippet", "")) for x in data.get("items", [])[:3]]

async def _serper(q):
    body = {"q": q, "num": 5}
    if SEARCH_COUNTRY:
        body["gl"] = SEARCH_COUNTRY
    async with HTTP["s"].post("https://google.serper.dev/search", json=body,
                              headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"}) as r:
        if r.status != 200:
            raise RuntimeError(f"serper {r.status}: {(await r.text())[:150]}")
        data = await r.json()
    out = []
    box = data.get("answerBox") or {}
    if box.get("answer") or box.get("snippet"):
        out.append((box.get("title", "Answer"), box.get("link", ""), box.get("answer") or box.get("snippet")))
    kg = data.get("knowledgeGraph") or {}
    if kg.get("description"):
        out.append((kg.get("title", ""), kg.get("descriptionLink", ""), kg["description"]))
    out += [(x.get("title", ""), x.get("link", ""), x.get("snippet", "")) for x in data.get("organic", [])]
    return out[:3]

async def _tavily(q):
    async with HTTP["s"].post("https://api.tavily.com/search",
                              json={"query": q, "max_results": 3, "search_depth": "basic"},
                              headers={"Authorization": f"Bearer {TAVILY_KEY}"}) as r:
        if r.status != 200:
            raise RuntimeError(f"tavily {r.status}: {(await r.text())[:150]}")
        data = await r.json()
    return [(x.get("title", ""), x.get("url", ""), x.get("content", "")) for x in data.get("results", [])]

DDG_BACKENDS = [b.strip() for b in (os.getenv("DDG_BACKENDS") or "auto,brave,yahoo,mojeek,wikipedia").split(",") if b.strip()]

def _ddg(q):
    """Free search through the ddgs library. DuckDuckGo often rate-limits servers (HTTP 202, no results),
    so try several of the library's other free backends before giving up."""
    last = None
    for backend in DDG_BACKENDS:
        try:
            with DDGS() as d:
                hits = d.text(q, max_results=3, backend=backend)
            if hits:
                return [(x.get("title", ""), x.get("href", ""), x.get("body", "")) for x in hits]
        except Exception as e:
            last = e
    if last:
        raise last
    return []

_search_cache = {}
_search_day = {"date": "", "count": 0}

def searches_today():
    return _search_day["count"] if _search_day["date"] == time.strftime("%Y-%m-%d") else 0

async def web_search(q):
    """Returns (compact text block, [urls]). Raises on failure."""
    q = q.strip()[:200]
    key = q.lower()
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1], hit[2]
    today = time.strftime("%Y-%m-%d")
    if _search_day["date"] != today:
        _search_day.update(date=today, count=0)
    if _search_day["count"] >= cfg["search_cap"]:
        raise RuntimeError("global daily search limit reached")
    results = []
    for eng in active_engines():
        try:
            if eng == "google":
                results = await _google(q)
            elif eng == "serper":
                results = await _serper(q)
            elif eng == "tavily":
                results = await _tavily(q)
            elif eng == "ddg":
                results = await asyncio.wait_for(asyncio.to_thread(_ddg, q), 20)
        except Exception as e:
            log.warning("%s search failed: %s", eng, e)
            results = []
        if results:
            break
    if not results:
        raise RuntimeError("no search results")
    _search_day["count"] += 1
    block = "\n".join(f"{i}. {t} | {c.strip()[:300]} ({u})" for i, (t, u, c) in enumerate(results, 1))
    urls = [u for _, u, _ in results if u]
    _search_cache[key] = (time.time(), block, urls)
    if len(_search_cache) > 200:
        _search_cache.pop(next(iter(_search_cache)))
    return block, urls


# ---------- calculator (safe, no eval) ----------
import ast, math, operator as _op

_OPS = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul, ast.Div: _op.truediv, ast.FloorDiv: _op.floordiv,
        ast.Mod: _op.mod, ast.Pow: _op.pow, ast.USub: _op.neg, ast.UAdd: _op.pos}
_FUNCS = {k: getattr(math, k) for k in ("sqrt", "sin", "cos", "tan", "log", "log10", "log2", "exp", "floor",
                                         "ceil", "factorial", "radians", "degrees")}
_FUNCS.update(abs=abs, round=round, min=min, max=max)
_CONST = {"pi": math.pi, "e": math.e}

def calc(expr):
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"\1/100*", expr, flags=re.I)
    expr = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", expr)

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 1000:
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in _CONST:
            return _CONST[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            args = [ev(a) for a in n.args]
            if n.func.id == "factorial" and args and args[0] > 500:
                raise ValueError("number too large")
            return _FUNCS[n.func.id](*args)
        raise ValueError("unsupported expression")

    result = ev(ast.parse(expr.strip(), mode="eval"))
    if isinstance(result, float):
        result = round(result, 10)
        if result.is_integer():
            result = int(result)
    return result
