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
PAGE_CHARS = 9000          # page text sent to the AI (~2.5k tokens)
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)

# ---------- SSRF guard ----------
PAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/131.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,bn;q=0.8"}

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
        async with HTTP["s"].get(url, allow_redirects=False, headers=PAGE_HEADERS,
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

LP_WAITS = [["--wait-until", "networkidle"], ["--wait-ms", "5000"], []]   # first one this build accepts is kept
_lp_wait = {"i": None}

async def _lightpanda(url, dump="markdown"):
    """Run the page's JavaScript and wait until it has loaded its data, then dump it."""
    async with _lp_sem:
        order = [_lp_wait["i"]] if _lp_wait["i"] is not None else range(len(LP_WAITS))
        last = ""
        for i in order:
            for flags in (LP_FLAGS, []) if LP_FLAGS else ([],):
                code, out, err = await _run_lp(["fetch", "--dump", dump, *LP_WAITS[i], *flags, url], 40)
                if code == 0 and out.strip():
                    if _lp_wait["i"] is None:
                        _lp_wait["i"] = i
                        log.info("Lightpanda waits with: %s", " ".join(LP_WAITS[i]) or "(default)")
                    return out
                last = err.strip()[-150:]
        raise RuntimeError(f"browser failed: {last}")

# ---------- backup reader: a real Chrome in the cloud (Jina Reader) ----------
JINA_KEY = env("JINA_API_KEY")

async def _jina(url):
    """r.jina.ai renders the page in headless Chrome and returns markdown. Free without a key (low rate)."""
    hdr = {"X-Return-Format": "markdown", "X-Timeout": "25", "X-With-Links-Summary": "false",
           "User-Agent": PAGE_HEADERS["User-Agent"]}
    if JINA_KEY:
        hdr["Authorization"] = f"Bearer {JINA_KEY}"
    async with HTTP["s"].get("https://r.jina.ai/" + url, headers=hdr,
                             timeout=aiohttp.ClientTimeout(total=40)) as r:
        if r.status != 200:
            raise RuntimeError(f"reader returned {r.status}")
        md = await r.text(errors="replace")
    md = re.sub(r"^(URL Source|Published Time|Warning):.*$", "", md, flags=re.M)
    m = re.search(r"^Title:\s*(.+)$", md, re.M)
    title = m.group(1).strip() if m else ""
    md = re.sub(r"^Title:.*$", "", md, flags=re.M).replace("Markdown Content:", "").strip()
    return title, md

# ---------- data hidden in the HTML (Next.js, Nuxt, JSON-LD, meta tags) ----------
def embedded_data(html, limit=6000):
    """Many JavaScript sites ship their content as JSON inside the page. Pull the readable parts out."""
    import json as _json
    out = []
    for k in ("og:title", "og:description", "description", "og:price:amount", "product:price:amount",
              "og:price:currency", "twitter:description"):
        m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)' % re.escape(k), html, re.I)
        if m:
            out.append(f"{k}: {m.group(1)}")
    blobs = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I)
    for sid in ("__NEXT_DATA__", "__NUXT_DATA__"):
        blobs += re.findall(r'<script[^>]+id=["\']%s["\'][^>]*>(.*?)</script>' % sid, html, re.S)
    blobs += re.findall(r"window\.__(?:INITIAL_STATE|APOLLO_STATE|PRELOADED_STATE|NUXT)__\s*=\s*(\{.*?\});?\s*</script>",
                        html, re.S)

    def walk(x, path, acc, depth=0):
        if len(acc) > 400 or depth > 12:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if k.startswith(("_", "@context")) or k in ("buildId", "runtimeConfig", "locales", "query"):
                    continue
                walk(v, f"{path}.{k}" if path else k, acc, depth + 1)
        elif isinstance(x, list):
            for i, v in enumerate(x[:60]):
                walk(v, f"{path}[{i}]", acc, depth + 1)
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):
            v = str(x).strip()
            if v and len(v) < 400 and not re.match(r"^(https?:|/|data:|[a-f0-9-]{20,}$)", v):
                acc.append(f"{path.split('.')[-1]}: {v}")
    for b in blobs[:6]:
        try:
            data = _json.loads(b.strip().rstrip(";"))
        except Exception:
            continue
        acc = []
        walk(data, "", acc)
        out.extend(acc)
    text = "\n".join(dict.fromkeys(out))
    return text[:limit]

SHELL_RE = re.compile(r"enable javascript|javascript is (required|disabled)|you need to enable|loading\.\.\.|"
                      r"please wait|checking your browser|just a moment|verify you are human|access denied", re.I)

def looks_empty(text):
    """An app shell, a bot check, or almost nothing: try the next reader."""
    t = (text or "").strip()
    return len(t) < 400 or (len(t) < 2500 and bool(SHELL_RE.search(t)))

def browser_name():
    return "Lightpanda" if LP["path"] else "HTTP reader"

_page_cache = {}

async def read_page(url, query=""):
    """Returns (title, text) of a public web page, trimmed for the AI to the parts that matter for `query`.
    Lightpanda (runs JavaScript, waits for data) -> real Chrome reader -> data hidden in the HTML -> plain HTTP."""
    title, text = await _read_any(url)
    return title, focus_text(text, query, PAGE_CHARS)

PRICE_HINT = re.compile(r"[৳$€£₹]|\b(tk|bdt|usd|price|taka|/mo|per month|buy|add to cart|sold|stock|plan)\b|\d[\d,.]*\s*%", re.I)

def focus_text(text, query="", budget=PAGE_CHARS):
    """Long page -> the start of the page plus the blocks that match the question or look like prices/products."""
    if len(text) <= budget:
        return text
    blocks = [b for b in re.split(r"\n(?=#)|\n{2,}|\n(?=[-*•|] )", text) if b.strip()]
    if len(blocks) < 4:
        blocks = [text[i:i + 600] for i in range(0, len(text), 600)]
    words = {w for w in re.findall(r"[a-z0-9\u0980-\u09ff]{3,}", (query or "").lower())} - {
        "the", "and", "what", "koto", "ache", "ase", "kono", "this", "that", "with", "from", "dam", "check", "site", "website",
        "https", "http", "www", "com", "xyz", "page", "read", "bolo", "tell", "about", "products", "product"}
    head = text[:min(1200, budget // 5)]
    scored = []
    for i, b in enumerate(blocks):
        bl = b.lower()
        sc = sum(3 for w in words if w in bl) + (2 if PRICE_HINT.search(b) else 0) + min(len(re.findall(r"\d", b)), 6) * .2
        scored.append((sc, i, b))
    keep, used = set(), len(head)
    for sc, i, b in sorted(scored, key=lambda x: (-x[0], x[1])):
        if sc <= 0 or used + len(b) > budget:
            continue
        keep.add(i)
        used += len(b)
    body = "\n".join(blocks[i].strip() for i in sorted(keep))
    return (head + "\n…\n" + body)[:budget] if body else text[:budget]

async def _read_any(url, limit=60000):
    url = url.rstrip(").,!?>")
    hit = _page_cache.get(url)
    if hit and time.time() - hit[0] < 900:
        return hit[1], hit[2]
    url = await final_url(url)                         # also blocks private / local addresses
    title, text, how = "", "", ""
    if LP["path"]:
        try:
            md = await _lightpanda(url)
            m = re.search(r"^#\s+(.+)$", md, re.M)
            title, text, how = (m.group(1).strip() if m else ""), squeeze(md), "browser"
        except BlockedURL:
            raise
        except Exception as e:
            log.warning("lightpanda failed for %s: %s", url, e)
    html = ""
    if looks_empty(text):
        try:
            _, ctype, body = await safe_get(url, max_bytes=3_000_000)
            if "html" in ctype or "<html" in body[:500].lower():
                html = body
                t2, x2 = html_to_text(body)
                extra = embedded_data(body)
                x2 = (x2 + ("\n\n[data in the page]\n" + extra if extra else "")).strip()
                if len(x2) > len(text):
                    title, text, how = t2 or title, x2, "html+data" if extra else "html"
            elif ctype.startswith("text/") or "json" in ctype:
                text, how = squeeze(body), "text"
            elif not text:
                raise RuntimeError("that link isn't a readable web page")
        except BlockedURL:
            raise
        except RuntimeError as e:
            if "readable" in str(e):
                raise
            log.info("plain read failed for %s: %s", url, e)
    if looks_empty(text):
        try:
            t3, x3 = await _jina(url)
            if len(squeeze(x3)) > len(text):
                title, text, how = t3 or title, squeeze(x3), "cloud browser"
        except Exception as e:
            log.info("cloud reader failed for %s: %s", url, e)
    if not text.strip():
        raise RuntimeError("the page had no readable text (it may need a login or block bots)")
    log.info("read %s via %s (%d chars)", url[:80], how or "?", len(text))
    text = text[:limit]
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
    """Full page content (JavaScript run, tables kept) for data extraction. Returns (final_url, title, text)."""
    url = await final_url(url.rstrip(").,!?>"))
    if LP["path"]:
        try:
            md = await _lightpanda(url)
            if not looks_empty(squeeze(md)):
                m = re.search(r"^#\s+(.+)$", md, re.M)
                return url, (m.group(1).strip() if m else ""), md[:limit]
        except BlockedURL:
            raise
        except Exception as e:
            log.warning("lightpanda raw failed for %s: %s", url, e)
    tables = ""
    try:
        _, ctype, body = await safe_get(url, max_bytes=3_000_000)
        if "html" in ctype or "<html" in body[:500].lower():
            t = _Tables()
            try:
                t.feed(body)
            except Exception:
                pass
            tables = "\n\n".join(t.tables)
    except Exception:
        pass
    _page_cache.pop(url, None)
    title, text = await _read_any(url, limit)
    return url, title, ((tables + "\n\n") if tables else "") + text[:limit]

async def screenshot(url):
    """PNG of a public page (after its JavaScript ran). Returns (final_url, png_bytes)."""
    url = await final_url(url.rstrip(").,!?>"))
    if LP["path"]:
        try:
            async with _lp_sem:
                proc = await asyncio.create_subprocess_exec(
                    LP["path"], "fetch", "--dump", "png", *(LP_WAITS[_lp_wait["i"] or 0]), url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, _ = await asyncio.wait_for(proc.communicate(), 45)
            if out[:8] == b"\x89PNG\r\n\x1a\n":
                return url, out
        except Exception as e:
            log.info("lightpanda screenshot failed: %s", e)
    hdr = {"X-Return-Format": "screenshot", "X-Timeout": "25"}
    if JINA_KEY:
        hdr["Authorization"] = f"Bearer {JINA_KEY}"
    async with HTTP["s"].get("https://r.jina.ai/" + url, headers=hdr, timeout=aiohttp.ClientTimeout(total=45)) as r:
        body = await r.read()
        if r.status != 200:
            raise RuntimeError(f"screenshot service returned {r.status}")
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return url, body
    m = re.search(rb"https://\S+?\.(?:png|jpe?g|webp)", body)
    if not m:
        raise RuntimeError("no screenshot returned")
    async with HTTP["s"].get(m.group().decode(), timeout=aiohttp.ClientTimeout(total=30)) as r:
        img = await r.read()
    if len(img) < 1000:
        raise RuntimeError("screenshot was empty")
    return url, img

# ---------- web search ----------
def engine_ok(name):
    return {"google": bool(GOOGLE_KEY and GOOGLE_CX), "serper": bool(SERPER_KEY),
            "tavily": bool(TAVILY_KEY), "ddg": DDGS is not None, "html": True}.get(name, False)

def active_engines():
    eng = [e for e in cfg["engines"] if engine_ok(e)]
    return eng + ([] if "html" in eng else ["html"])          # free results-page fallback, always last

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0 Safari/537.36")

def _strip_tags(x):
    import html as _h
    return _h.unescape(re.sub(r"<[^>]+>", "", x or "")).strip()

async def _html_search(q):
    """No API key needed: read Bing's / DuckDuckGo's normal results page."""
    from urllib.parse import quote_plus, unquote, parse_qs, urlparse as _up
    import base64 as _b64
    hdr = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    tries = [("bing", f"https://www.bing.com/search?q={quote_plus(q)}&setlang=en&cc=us"),
             ("ddg", f"https://html.duckduckgo.com/html/?q={quote_plus(q)}")]
    for name, url in tries:
        try:
            async with HTTP["s"].get(url, headers=hdr, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status != 200:
                    continue
                body = await r.text(errors="replace")
        except Exception as e:
            log.info("html search %s failed: %s", name, e)
            continue
        out = []
        if name == "bing":
            for blk in re.findall(r'<li class="b_algo".*?</li>', body, re.S)[:5]:
                a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
                p = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S) or re.search(r'class="b_caption"[^>]*>(.*?)</div>', blk, re.S)
                if a:
                    link = a.group(1)
                    if "bing.com/ck/a" in link:            # Bing wraps links: u=a1<base64url>
                        u = parse_qs(_up(link.replace("&amp;", "&")).query).get("u", [""])[0]
                        if u.startswith("a1"):
                            try:
                                link = _b64.urlsafe_b64decode(u[2:] + "==").decode()
                            except Exception:
                                pass
                    out.append((_strip_tags(a.group(2)), link, _strip_tags(p.group(1)) if p else ""))
            # Bing's answer box (exchange rates, sums, weather …)
            box = re.search(r'<div[^>]+class="[^"]*(?:b_focusTextLarge|b_focusTextMedium|b_ans)[^"]*"[^>]*>(.*?)</div>', body, re.S)
            if box and _strip_tags(box.group(1)):
                out.insert(0, ("Bing answer", url, _strip_tags(box.group(1))[:300]))
        else:
            for a, snip in re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>.*?class="result__snippet"[^>]*>(.*?)</a>',
                                      body, re.S)[:5]:
                link = unquote(parse_qs(_up(a.replace("&amp;", "&")).query).get("uddg", [a])[0])
                out.append((link.split("/")[2] if "//" in link else link, link, _strip_tags(snip)))
        out = [(t, u, b) for t, u, b in out if u.startswith("http")]
        if out:
            return out[:4]
    return []

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
            elif eng == "html":
                results = await _html_search(q)
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
