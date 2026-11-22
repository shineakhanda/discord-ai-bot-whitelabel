"""Agentic tools: a real browser the bot can drive, structured page components (products, prices, stock), vision
(Gemma looks at photos and screenshots), a small step-runner script for multi-step tool work, and an embed builder."""
import base64, json, re, time
from urllib.parse import urlparse

import discord
from discord import app_commands

import ai, browser, store, web
from store import log, cfg
from core import client, tree, is_staff, safe_send, on_startup, feature
from agent import tool, Result, TOOLS, available, run_tool, split_args

URL_RE = re.compile(r"https?://[^\s<>|]+", re.I)


def _url(text):
    m = URL_RE.search(text or "")
    if m:
        return m.group().rstrip(").,!?>")
    t = (text or "").strip().split()[0] if (text or "").strip() else ""
    if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?$", t, re.I):
        return "https://" + t
    return ""


def _browse_ok(c):
    return feature(c.gid, "browse")


# ---------------------------------------------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------------------------------------------
@tool("browse", "browse | <url> | <steps, optional: e.g. 'click Minecraft; wait 2; type search=cape; scroll'>",
      "open a website in a real browser (React/Next/Vue sites work), optionally click tabs/categories, search, scroll, "
      "then read everything: products, prices, old prices, stock, buttons. Use for store/category pages and anything "
      "that needs clicking. Never clicks buy/login/submit.", when=_browse_ok, progress="🧭 Opening the site in a browser…")
async def t_browse(c, a):
    url = _url(a[0] if a else "")
    if not url:
        return Result("Give me a website link to open.")
    steps = " | ".join(a[1:]).strip()
    if browser.ready():
        try:
            data = await browser.render(url, steps)
        except web.BlockedURL as e:
            return Result(str(e))
        except Exception as e:
            log.warning("browse %s failed: %s", url, e)
            data = None
        if data:
            text = web.focus_text(browser.as_text(data), c.query or steps, 12000)
            n = len(data.get("items") or [])
            return Result(f"Page: {data.get('url') or url}\nTitle: {data.get('title', '')}\n\n{text}",
                          f"🧭 browsed {urlparse(url).netloc or url[:40]}"
                          + (f" · {n} items" if n else ""), [data.get("url") or url])
    # no Chrome on this host: best reader available (no clicking)
    try:
        title, text = await web.read_page(url, c.query or steps)
    except Exception as e:
        return Result(f"Couldn't open {url}: {e}")
    note = "\n(Note: clicking isn't available on this host; this is the page as loaded.)" if steps else ""
    return Result(f"Page: {url}\nTitle: {title}\n\n{text}{note}", "🌐 read the site", [url])


@tool("page_items", "page_items | <url> | <filter words, optional, e.g. 'cape' or 'nitro'>",
      "list every product/plan card on a page with exact price, old price and stock status (in stock / sold out / "
      "N left). Best for 'what's in stock', 'price of X on the site'.", when=_browse_ok,
      progress="🛒 Reading products and stock…")
async def t_items(c, a):
    url = _url(a[0] if a else "")
    if not url:
        return Result("Give me the store link.")
    want = [w for w in re.findall(r"\w{3,}", (" ".join(a[1:]) or "").lower())]
    items = []
    if browser.ready():
        try:
            data = await browser.render(url)
            items = data.get("items") or []
        except Exception as e:
            log.info("page_items chrome failed: %s", e)
    if not items:                                   # sitebrain index or page text as fallback
        title, text = await web.read_page(url, " ".join(want) or "price stock")
        return Result(f"(No product cards could be read as components here — page text instead.)\n{text}",
                      "🌐 read the site", [url])
    if want:
        hit = [it for it in items if any(w in (it.get("name", "") + " " + it.get("text", "")).lower() for w in want)]
        items = hit or items
    instock = sum(1 for it in items if re.search(r"in.?stock|available|\d+\s+left", it.get("stock", ""), re.I)
                  and not re.search(r"out|unavail|sold", it.get("stock", ""), re.I))
    soldout = sum(1 for it in items if re.search(r"out of stock|sold.?out|unavailable", it.get("stock", ""), re.I))
    head = f"{len(items)} item(s) on {url}" + (f" · {instock} in stock · {soldout} sold out" if instock or soldout else "")
    return Result(head + "\n" + browser.format_items(items, 60), f"🛒 read {len(items)} items", [url])


# ---------------------------------------------------------------------------------------------------------------
# vision
# ---------------------------------------------------------------------------------------------------------------
VISION_MODELS = ["g:gemma-4-26b-a4b-it", "g:gemma-4-31b-it"]

async def vision(question, images, max_tokens=900):
    """Ask a vision model about images: list of (bytes, mime). Returns text."""
    models = [m for m in ([cfg.get("vision_model")] + VISION_MODELS) if m]
    usable = [m for m in models if "image" in await ai.model_inputs(m)]
    if not usable:
        raise RuntimeError("no vision model is set up (needs a Google AI key or a vision_model)")
    parts = [{"type": "text", "text": question}]
    for data, mime in images[:4]:
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    msgs = [{"role": "system", "content": "You look at images carefully and describe exactly what is visible: text, "
             "prices, numbers, errors, buttons, layout. Never guess what isn't visible. Reply in the user's language."},
            {"role": "user", "content": parts}]
    reply, _ = await ai.ask(msgs, max_tokens, models=usable, temperature=0.2)
    return reply


async def _attached_images(c):
    msgs = [m for m in (getattr(c, "message", None),) if m]
    ref = getattr(getattr(c.message, "reference", None), "resolved", None) if c.message else None
    if isinstance(ref, discord.Message):
        msgs.append(ref)
    out = []
    for m in msgs:
        for att in m.attachments:
            ctype = (att.content_type or "").split(";")[0]
            if ctype.startswith("image/") and att.size <= 6_000_000:
                out.append((await att.read(), ctype))
    return out[:4]


@tool("look", "look | <image link, website link, or 'photo' for the attached image> | <what to check>",
      "SEE with vision: read text/prices/errors in a photo or screenshot, or screenshot a website and look at it "
      "(useful when page text isn't enough: banners, images with prices, error screens).",
      progress="👁️ Looking…")
async def t_look(c, a):
    target = (a[0] if a else "").strip()
    question = " | ".join(a[1:]).strip() or c.query or "Describe what you see, including all text and numbers."
    images, label = [], "👁️ looked"
    url = _url(target)
    if not url or target.lower() in ("photo", "image", "attachment", "screenshot", "this"):
        images = await _attached_images(c)
        label = "👁️ viewed the photo"
    if not images and url:
        try:
            _, ctype, _ = await web.safe_get(url, max_bytes=64)
        except Exception:
            ctype = ""
        if ctype.startswith("image/"):
            import aiohttp
            async with web.HTTP["s"].get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30)) as r:
                images = [(await r.read(), ctype)]
            label = "👁️ viewed the image"
        else:
            try:
                final, png = await web.screenshot(url)
                images = [(png, "image/png")]
                label = f"👁️ looked at {url[:50]}"
            except Exception as e:
                return Result(f"Couldn't take a screenshot of {url}: {e}")
    if not images:
        return Result("There's no image to look at — ask them to attach it, or give a link.")
    try:
        seen = await vision(question, images)
    except Exception as e:
        return Result(f"Vision failed: {e}")
    return Result(f"What the image shows (seen with vision):\n{seen}", label)


# ---------------------------------------------------------------------------------------------------------------
# step runner: several dependent tool calls in one go, executed by code (no AI between steps)
# ---------------------------------------------------------------------------------------------------------------
MAX_STEPS = 8

def parse_script(text):
    steps = []
    for line in re.split(r"\n|;;", text or ""):
        line = line.strip().lstrip("-•*0123456789.) ").strip()
        if not line or line.startswith("#"):
            continue
        name, _, arg = line.partition("|")
        name = name.strip().lower().replace(" ", "_")
        if not re.match(r"^[a-z_]+$", name):
            continue
        steps.append((name, arg.strip()))
    return steps[:MAX_STEPS]

async def run_script(c, text, allowed=None):
    """Run 'tool | input' lines in order. $1, $2… insert earlier results (first 1500 chars), $last = the last one."""
    allowed = allowed if allowed is not None else {t.name for t in available(c)} - {"run_steps"}
    outs, report = [], []
    for i, (name, arg) in enumerate(parse_script(text), 1):
        if name not in allowed:
            report.append(f"{i}. {name}: not allowed here — skipped")
            outs.append("")
            continue
        def sub(m):
            k = m.group(1)
            idx = len(outs) if k == "last" else int(k)
            return outs[idx - 1][:1500] if 0 < idx <= len(outs) else ""
        arg = re.sub(r"\$(last|\d+)", sub, arg)
        t0 = time.monotonic()
        res = await run_tool(c, name, arg)
        outs.append(res.text)
        report.append(f"{i}. {name} | {arg[:80]} ({time.monotonic() - t0:.1f}s)\n{res.text[:2500]}")
    return "\n\n".join(report) or "No valid steps. Write one per line: tool | input"

@tool("run_steps", "run_steps | <steps, one per line: tool | input — use $1, $2 or $last for earlier results>",
      "run several tool calls in order in ONE call (code runs them, results flow into the next step with $1/$last). "
      "Example: 'page_items | blockifystore.xyz | cape\\ncalc | 450*3'. Use for multi-step tasks.",
      progress="⚙️ Running steps…")
async def t_steps(c, a):
    text = " | ".join(a)
    text = text.replace("\\n", "\n")
    return Result(await run_script(c, text), "⚙️ ran steps")


# ---------------------------------------------------------------------------------------------------------------
# embeds
# ---------------------------------------------------------------------------------------------------------------
COLORS = {"red": 0xEF4444, "green": 0x22C55E, "blue": 0x3B82F6, "yellow": 0xEAB308, "orange": 0xF97316,
          "purple": 0x8B5CF6, "pink": 0xEC4899, "black": 0x111827, "white": 0xF9FAFB, "gold": 0xF59E0B,
          "teal": 0x14B8A6, "gray": 0x6B7280, "grey": 0x6B7280}

def parse_color(text):
    t = (text or "").strip().lower().lstrip("#")
    if t in COLORS:
        return COLORS[t]
    if re.fullmatch(r"(0x)?[0-9a-f]{6}", t):
        return int(t[-6:], 16)
    return None

def build_embed(title="", body="", color=None, image="", footer="", thumb=""):
    body = (body or "").replace("\\n", "\n")
    fields = []
    # lines like "## Field name" start a field; text until the next "## " is its value
    m = re.split(r"(?m)^##\s+(.+)$", body)
    if len(m) > 1:
        desc = m[0].strip()
        for i in range(1, len(m) - 1, 2):
            fields.append((m[i].strip()[:256], (m[i + 1].strip() or "​")[:1024]))
    else:
        desc = body.strip()
    e = discord.Embed(title=(title or None) and title[:256], description=desc[:4096] or None,
                      color=color if color is not None else 0x5865F2)
    for name, val in fields[:25]:
        inline = len(val) < 60 and "\n" not in val
        e.add_field(name=name, value=val, inline=inline)
    if image and _url(image):
        e.set_image(url=_url(image))
    if thumb and _url(thumb):
        e.set_thumbnail(url=_url(thumb))
    if footer:
        e.set_footer(text=footer[:2048])
    return e

def _staff(c):
    return bool(c.gid and c.guild and is_staff(c.user, c.gid))

def _target_channel(c, text):
    t = (text or "").strip().lower()
    if not t or t in ("here", "this", "this channel"):
        return c.channel
    from agent import find_channel
    return find_channel(c.guild, text)

@tool("embed_post", "embed_post | <#channel or 'here'> | <title> | <body: markdown; '## Name' lines start fields> | "
      "<color: name or hex, optional> | <image url, optional> | <footer, optional>",
      "(staff) design and post a rich embed (announcement, price list, rules, plan card) in a channel",
      when=_staff, progress="🎨 Building the embed…")
async def t_embed(c, a):
    a = a + [""] * (6 - len(a))
    ch = _target_channel(c, a[0])
    if ch is None:
        return Result(f"Channel '{a[0]}' not found.")
    perms = ch.permissions_for(c.user) if hasattr(ch, "permissions_for") else None
    if perms is not None and not (perms.send_messages and perms.embed_links):
        return Result(f"You can't post embeds in #{ch.name}.")
    e = build_embed(a[1], a[2], parse_color(a[3]), a[4], a[5])
    if not (e.title or e.description or e.fields):
        return Result("The embed needs a title or body.")
    msg = await safe_send(ch, embed=e)
    if not msg:
        return Result(f"Discord refused the embed in #{ch.name} (check my Embed Links permission).")
    return Result(f"Posted the embed in #{ch.name}: {msg.jump_url}", f"🎨 posted embed in #{ch.name}", [msg.jump_url])


class EmbedModal(discord.ui.Modal, title="Create an embed"):
    e_title = discord.ui.TextInput(label="Title", required=False, max_length=256)
    e_body = discord.ui.TextInput(label="Body (markdown; '## Name' starts a field)", style=discord.TextStyle.paragraph,
                                  required=False, max_length=4000)
    e_color = discord.ui.TextInput(label="Color (name or hex)", required=False, max_length=16, placeholder="blue / #22c55e")
    e_image = discord.ui.TextInput(label="Image URL", required=False, max_length=400)
    e_footer = discord.ui.TextInput(label="Footer", required=False, max_length=200)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, inter: discord.Interaction):
        e = build_embed(self.e_title.value, self.e_body.value, parse_color(self.e_color.value), self.e_image.value,
                        self.e_footer.value)
        if not (e.title or e.description or e.fields):
            return await inter.response.send_message("Add a title or body.", ephemeral=True)
        try:
            msg = await self.channel.send(embed=e)
        except discord.HTTPException as ex:
            return await inter.response.send_message(f"Couldn't post: {ex.text[:150]}", ephemeral=True)
        await inter.response.send_message(f"✅ Posted: {msg.jump_url}", ephemeral=True)


@tree.command(name="embed", description="Create and post a rich embed (staff)")
@app_commands.describe(channel="Where to post it (default: here)")
async def embed_cmd(inter: discord.Interaction, channel: discord.TextChannel = None):
    if not inter.guild or not is_staff(inter.user, inter.guild.id):
        return await inter.response.send_message("Only staff can post embeds.", ephemeral=True)
    ch = channel or inter.channel
    p = ch.permissions_for(inter.user)
    if not (p.send_messages and p.embed_links):
        return await inter.response.send_message(f"You can't post embeds in {ch.mention}.", ephemeral=True)
    await inter.response.send_modal(EmbedModal(ch))
