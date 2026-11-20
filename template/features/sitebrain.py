"""Site brain: learn a whole website (JavaScript sites too) and answer from it.

An admin says "scan blockifystore.xyz and save it" -> the bot reads the sitemap and follows links on the same
site (each page rendered by the browser), saves every page, and from then on uses the matching pages to answer
product / price / info questions. Re-run any time to refresh.
"""
import re, time, asyncio
from urllib.parse import urlparse, urljoin

import discord

import store, web
from store import db, log
from agent import tool, Result

db.executescript("""
CREATE TABLE IF NOT EXISTS site_pages (guild_id INTEGER, url TEXT, host TEXT, title TEXT, text TEXT, ts REAL,
                                       PRIMARY KEY (guild_id, url));
CREATE INDEX IF NOT EXISTS site_pages_g ON site_pages (guild_id, host);
CREATE TABLE IF NOT EXISTS site_items (guild_id INTEGER, host TEXT, name TEXT, price TEXT, url TEXT, extra TEXT,
                                       PRIMARY KEY (guild_id, host, name, price));
""")
db.commit()

SKIP = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|json|xml|pdf|zip|rar|mp4|mp3|woff2?|ttf)(\?|$)|"
                  r"/(login|signin|sign-in|register|signup|logout|cart|checkout|account|wp-admin|admin|auth|api)/?|"
                  r"[?&](add-to-cart|share|replytocom)=|#", re.I)
GOOD = re.compile(r"/(product|products|shop|store|item|items|p|category|categories|collections?|pricing|plans?|"
                  r"packages?|services?|faq|about|contact|terms|refund|policy|marketplace)\b", re.I)
MAX_PAGES = 40
_running = set()


PRICE_RE = re.compile(r"(?:[$৳€£₹]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:tk|bdt|usd|taka|৳|\$)(?![a-z]))", re.I)
NOT_NAME = re.compile(r"^(total|subtotal|price|sale|from|only|save|discount|was|now|regular|starting|per|each|add to cart|"
                      r"buy now|in stock|out of stock|sold out|qty|quantity|home|cart|\W*)$", re.I)

def _clean(line):
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    return re.sub(r"[#*_>`|•\-–—]+", " ", line).strip()

def extract_items(text, url):
    """(name, price, extra) pairs from a page: 'COPPER CAPE' + '$0.04', 'INTEL-48 … ৳3,826/mo', table rows."""
    lines = [_clean(l) for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    out = []
    for i, l in enumerate(lines):
        for m in PRICE_RE.finditer(l):
            name = _clean(l[:m.start()]).strip(" :=")
            if len(re.findall(r"[A-Za-z\u0980-\u09ff]", name)) < 3 or NOT_NAME.match(name):
                j = i - 1
                while j >= 0 and (PRICE_RE.search(lines[j]) is None) and len(lines[j]) < 3:
                    j -= 1
                name = lines[j] if j >= 0 and not PRICE_RE.search(lines[j]) else ""
            name = re.sub(r"\s+", " ", name)[:90]
            if 3 <= len(name) <= 90 and not NOT_NAME.match(name):
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                extra = nxt[:80] if re.search(r"stock|deliver|month|/mo|day|gb|core|ram|ssd|nvme|warranty", nxt, re.I) \
                    and not PRICE_RE.search(nxt) else ""
                out.append((name, m.group().strip(), extra))
            break
    return out[:200]

def _same_site(u, host):
    h = urlparse(u).netloc.lower().removeprefix("www.")
    return h == host


def _links(base, md_or_html, host):
    found = re.findall(r"\]\((https?://[^)\s]+|/[^)\s]*)\)", md_or_html) + re.findall(r'href=["\']([^"\'#]+)', md_or_html)
    out = []
    for u in found:
        u = urljoin(base, u).split("#")[0].rstrip("/")
        if u.startswith("http") and _same_site(u, host) and not SKIP.search(u):
            out.append(u)
    return out


async def _sitemap(root, host):
    urls = []
    for path in ("/sitemap.xml", "/sitemap_index.xml", "/product-sitemap.xml"):
        try:
            _, _, body = await web.safe_get(root + path, max_bytes=2_000_000)
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        for loc in locs:
            if loc.endswith(".xml") and len(urls) < 400:          # nested sitemaps
                try:
                    _, _, sub = await web.safe_get(loc, max_bytes=2_000_000)
                    urls += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub)
                except Exception:
                    pass
            else:
                urls.append(loc)
        if urls:
            break
    return [u.rstrip("/") for u in urls if _same_site(u, host) and not SKIP.search(u)]


async def crawl(gid, start, limit=MAX_PAGES, progress=None):
    """Read up to `limit` pages of the site. Returns (host, pages_saved, failed)."""
    start = await web.final_url(start)
    p = urlparse(start)
    host = p.netloc.lower().removeprefix("www.")
    root = f"{p.scheme}://{p.netloc}"
    queue = [start.rstrip("/")]
    sm = await _sitemap(root, host)
    queue += sorted(sm, key=lambda u: (not GOOD.search(u), len(u)))
    seen, saved, failed = set(), 0, 0
    sem = asyncio.Semaphore(2)

    async def one(u):
        nonlocal saved, failed
        async with sem:
            try:
                md, title, text = "", "", ""
                if web.LP["path"]:
                    try:
                        await web.check_url(u, (80, 443))
                        md = await web._lightpanda(u)
                    except web.BlockedURL:
                        raise
                    except Exception:
                        md = ""
                if md and not web.looks_empty(web.squeeze(md)):
                    m = re.search(r"^#\s+(.+)$", md, re.M)
                    title, text = (m.group(1).strip() if m else ""), web.squeeze(md)
                else:
                    title, text = await web._read_any(u, 40000)
                for link in _links(u, md, host):
                    if link not in seen and len(seen) + len(queue) < limit * 4:
                        queue.append(link)
                if len(text) > 80:
                    db.execute("INSERT OR REPLACE INTO site_pages VALUES (?, ?, ?, ?, ?, ?)",
                               (gid, u, host, title[:200], text[:15000], time.time()))
                    items = extract_items(md or text, u)
                    if len(items) == 1 and title:          # a product page: its own title is the product name
                        items = [(title.split("|")[0].strip()[:90], items[0][1], items[0][2])]
                    for name, price, extra in items:
                        db.execute("INSERT OR REPLACE INTO site_items VALUES (?, ?, ?, ?, ?, ?)",
                                   (gid, host, name, price, u, extra))
                    db.commit()
                    saved += 1
            except web.BlockedURL:
                failed += 1
            except Exception as e:
                failed += 1
                log.info("site crawl: %s failed: %s", u, str(e)[:100])

    while queue and len(seen) < limit:
        batch = []
        while queue and len(batch) < 4 and len(seen) < limit:
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            batch.append(u)
        if not batch:
            break
        await asyncio.gather(*(one(u) for u in batch))
        if progress:
            try:
                await progress(len(seen), saved)
            except Exception:
                pass
    return host, saved, failed


def sites(gid):
    return db.execute("SELECT host, COUNT(*), MAX(ts) FROM site_pages WHERE guild_id=? GROUP BY host",
                      (gid,)).fetchall()


def item_matches(gid, query, limit=8):
    """Products whose name matches the question, with their exact price and page."""
    words = [w for w in re.findall(r"[a-z0-9\u0980-\u09ff]{2,}", (query or "").lower())
             if w not in {"the", "er", "ki", "koto", "dam", "price", "of", "what", "is", "ache", "kono", "bolo", "tell",
                          "me", "support", "and", "ar", "how", "much", "taka"}]
    if not words:
        return []
    rows = db.execute("SELECT name, price, url, extra FROM site_items WHERE guild_id=?", (gid,)).fetchall()
    scored = []
    for name, price, url, extra in rows:
        nl = name.lower()
        hits = sum(1 for w in words if w in nl)
        if hits:
            scored.append((hits / max(1, len(nl.split())) + hits, name, price, url, extra))
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0] if scored else 0
    seen, out = set(), []
    for sc, name, price, url, extra in scored:
        key = (name.lower(), price)
        if sc >= best * 0.6 and key not in seen:
            seen.add(key)
            out.append((name, price, url, extra))
        if len(out) >= limit:
            break
    return out

def site_context(gid, query, budget=7000):
    """Exact product matches first, then the saved pages that best match the question."""
    items = item_matches(gid, query)
    head = ""
    if items:
        head = ("PRODUCT MATCHES — exact name and price as shown on the website (use THESE prices):\n"
                + "\n".join(f"- {n} — {p}" + (f" ({e})" if e else "") + f" — {u}" for n, p, u, e in items) + "\n\n")
    rows = db.execute("SELECT url, title, text FROM site_pages WHERE guild_id=?", (gid,)).fetchall()
    if not rows:
        return ""
    words = {w for w in re.findall(r"[a-z0-9ঀ-৿]{3,}", (query or "").lower())} - {
        "the", "and", "koto", "ache", "dam", "price", "what", "kono", "this", "website", "site", "product", "products"}
    scored = []
    for url, title, text in rows:
        tl, ul = (title or "").lower(), url.lower()
        sc = sum(4 * (w in tl) + 3 * (w in ul) + min(text.lower().count(w), 5) for w in words)
        sc += 1 if web.PRICE_HINT.search(text[:3000]) else 0
        scored.append((sc, url, title, text))
    scored.sort(key=lambda x: -x[0])
    top = [s for s in scored if s[0] > 0][:4] or scored[:2]
    per = max(1200, budget // max(1, len(top)))
    out = [f"## {title or url}\n{url}\n{web.focus_text(text, query, per)}" for _, url, title, text in top]
    return (head + "\n\n".join(out))[:budget + 1500]


def _admin(c):
    from core import is_admin
    return bool(c.gid and is_admin(c.user))


@tool("learn_site", "learn_site | <website url> | <max pages, optional>",
      "(admins) read a whole website (JavaScript sites too) and save it, so product / price / info questions are "
      "answered from it. Use when asked to scan, learn, save or remember a website. Re-run to refresh.",
      when=_admin, progress="🌐 Learning the website **{0}**…")
async def t_learn_site(c, a):
    m = web.URL_RE.search(a[0] if a else "") or re.search(r"[\w-]+(\.[\w-]+)+", a[0] if a else "")
    if not m:
        return Result("Give me the website address.")
    url = m.group() if m.group().startswith("http") else "https://" + m.group()
    limit = max(3, min(MAX_PAGES, int(re.sub(r"\D", "", a[1]) or MAX_PAGES))) if len(a) > 1 and a[1] else 25
    key = (c.gid, urlparse(url).netloc)
    if key in _running:
        return Result("I'm already learning that website — I'll post when it's done.")
    _running.add(key)
    channel = c.channel

    async def run():
        try:
            host, saved, failed = await crawl(c.gid, url, limit)
            if channel:
                await channel.send(f"🧠 Learned **{saved} pages** from **{host}**"
                                   + (f" ({failed} couldn't be read)" if failed else "")
                                   + ". Ask me about its products, prices or info any time.",
                                   allowed_mentions=discord.AllowedMentions.none())
        except web.BlockedURL:
            if channel:
                await channel.send("That address isn't allowed (private or local network).")
        except Exception as e:
            log.warning("learn_site failed: %s", e)
            if channel:
                await channel.send(f"⚠️ Couldn't finish learning that website: {str(e)[:150]}")
        finally:
            _running.discard(key)

    asyncio.get_running_loop().create_task(run())
    return Result(f"Started reading {url} (up to {limit} pages, JavaScript pages included). It runs in the "
                  "background (about 1–3 minutes) and I'll post here when it's saved. Tell the user that, briefly.",
                  "🌐 learning website")


@tool("forget_site", "forget_site | <website>", "(admins) delete a website the bot learned", when=_admin,
      progress="🧹 Forgetting the website…")
async def t_forget_site(c, a):
    host = urlparse(a[0] if "//" in a[0] else "https://" + a[0]).netloc.lower().removeprefix("www.")
    n = db.execute("DELETE FROM site_pages WHERE guild_id=? AND host=?", (c.gid, host)).rowcount
    db.commit()
    return Result(f"Deleted {n} saved pages of {host}.", "🧹 forgot website" if n else None)
