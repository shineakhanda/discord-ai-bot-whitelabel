"""LLM layer: OpenRouter calls, tool-call parsing, model capabilities, short-term chat memory."""
import re, json, time, asyncio, contextvars
from collections import defaultdict, deque

import aiohttp

from store import cfg, OR_KEY, AI_BASE_URL, AI_API_KEY, GOOGLE_AI_KEY, GOOGLE_BASE, HTTP, env, log

API = "https://openrouter.ai/api/v1"            # OpenRouter (default, and backup with the "or:" prefix)

# ---------- providers ----------
# Model names: "some-model"       → main provider (AI_BASE_URL if set, else OpenRouter)
#              "or:vendor/model"  → always OpenRouter (handy as a free backup)
#              "g:gemma-4-31b-it" → Google AI Studio (GOOGLE_AI_KEY)
def provider(model):
    """(base_url, api_key, model_id, is_openrouter) for a model name."""
    if model.startswith("g:"):                     # Google AI Studio (Gemma / Gemini)
        return GOOGLE_BASE, GOOGLE_AI_KEY, model[2:], False
    if model.startswith("or:") or not AI_BASE_URL:
        return API, OR_KEY, model.removeprefix("or:"), True
    return AI_BASE_URL, AI_API_KEY, model, "openrouter.ai" in AI_BASE_URL

def provider_name():
    if cfg.get("model", "").startswith("g:"):
        return "Google AI Studio"
    if not AI_BASE_URL:
        return "OpenRouter"
    return "numes.ai" if "numes.ai" in AI_BASE_URL else AI_BASE_URL.split("//")[-1].split("/")[0]

# what the main (non-OpenRouter) provider's models accept, e.g. AI_INPUTS=text,image,audio
DECLARED_INPUTS = {m.strip() for m in (env("AI_INPUTS") or "text").split(",") if m.strip()}
CTX_TTL = 12 * 60 * 60               # conversation memory per channel (saved in the database)

TOOL_RE = re.compile(r"^[\s`*>_]*TOOL[\s*_]*:[\s*_]*([a-z_]+)[\s*_]*(?:\|\s*(.*))?$", re.I)

def parse_tool(reply):
    """('name', 'input') if the model asked for a tool, else None."""
    for line in reply.strip().splitlines()[:3]:
        line = line.strip().strip("`").strip()
        if not line:
            continue
        m = TOOL_RE.match(line)
        if m:
            return m.group(1).lower(), (m.group(2) or "").strip().strip("`")
        if line.upper().startswith("SEARCH:"):            # legacy format
            return "search", line.split(":", 1)[1].strip()
        return None
    return None

# =====================================================================
# chat memory (per channel, 12h, survives restarts)
# =====================================================================
ctx = defaultdict(lambda: deque(maxlen=cfg["max_turns"] * 2))

def _q(key):
    if key not in ctx:
        import store
        ctx[key].extend(store.ctx_load(key, time.time() - CTX_TTL, cfg["max_turns"] * 2))
    return ctx[key]

def history(key):
    q = _q(key)
    cutoff = time.time() - CTX_TTL
    while q and q[0][0] < cutoff:
        q.popleft()
    return [{"role": r, "content": c} for _, r, c in q]

def remember(key, user_line, reply):
    import store
    q, now = _q(key), time.time()
    for role, text in (("user", user_line), ("assistant", reply)):
        q.append((now, role, text))
        try:
            store.ctx_add(key, now, role, text)
        except Exception as e:
            log.warning("chat memory save failed: %s", e)

def forget(key):
    import store
    ctx.pop(key, None)
    store.ctx_clear(key)

def resize_ctx():
    n = cfg["max_turns"] * 2
    for k in list(ctx):
        ctx[k] = deque(ctx[k], maxlen=n)
    ctx.default_factory = lambda: deque(maxlen=cfg["max_turns"] * 2)

def prune_ctx():
    cutoff = time.time() - CTX_TTL
    for k in list(ctx):
        q = ctx[k]
        if not q or q[-1][0] < cutoff:
            del ctx[k]
    try:
        import store
        store.ctx_prune(cutoff)
    except Exception:
        pass

# =====================================================================
# model capabilities (image / audio input)
# =====================================================================
_caps = {}

async def model_inputs(model):
    """Set of input modalities for a model, e.g. {'text','image','audio'}. Cached 6h."""
    hit = _caps.get(model)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    base, key, mid, is_or = provider(model)
    if model.startswith("g:"):                     # Gemma 4 / Gemini read images
        return {"text", "image"}
    if not is_or:                                  # other providers don't publish this; use AI_INPUTS
        return set(DECLARED_INPUTS) | {"text"}
    mods = {"text"}
    try:
        async with HTTP["s"].get(f"{API}/model/{mid}", headers={"Authorization": f"Bearer {key}"},
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                data = (await r.json(content_type=None)).get("data", {})
                mods = set((data.get("architecture") or {}).get("input_modalities") or ["text"])
    except Exception as e:
        log.warning("capability lookup failed for %s: %s", model, e)
    _caps[model] = (time.time(), mods)
    return mods

async def pick_model(need):
    """Model list to use for a message needing 'image'/'audio' input. None = not possible."""
    if not need:
        return None
    if set(need) <= await model_inputs(cfg["model"]):
        return [cfg["model"]]
    for key in ("vision_model" if "image" in need else None, "audio_model" if "audio" in need else None):
        m = cfg.get(key) if key else ""
        if m and set(need) <= await model_inputs(m):
            return [m]
    return []

def clear_caps():
    _caps.clear()

# =====================================================================
# OpenRouter
# =====================================================================
# hidden reasoning some models print in the reply: <think>, <thinking>, <thought> (Gemma), <reasoning>, channel markers
THINK_TAGS = r"(?:think|thinking|thought|thoughts|reasoning)"
THINK_RE = re.compile(rf"<{THINK_TAGS}>.*?</{THINK_TAGS}>", re.S | re.I)
THINK_END = re.compile(rf"</{THINK_TAGS}>", re.I)
CHANNEL_RE = re.compile(r"<\|channel\|?>\s*thought.*?(?:<\|channel\|?>\s*(?:final|answer)|<channel\|>)", re.S | re.I)

class APIError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"{status}: {msg}")
        self.status = status

def clean(text):
    text = CHANNEL_RE.sub("", THINK_RE.sub("", text))
    parts = THINK_END.split(text)                 # closing tag without an opening one: keep what comes after
    if len(parts) > 1:
        text = parts[-1]
    text = re.sub(rf"^\s*<{THINK_TAGS}>.*\Z", "", text, flags=re.S | re.I)   # reply cut off mid-thought
    return text.strip()

# How each provider wants thinking switched off/on. Tried in order; the first one the provider
# accepts is remembered (numes.ai, DeepSeek, Groq… all name it differently).
THINK_STYLES = ["reasoning", "effort", "thinking", None]
GOOGLE_STYLES = ["google", "effort_min", None]     # Gemma 4 on Google: thinking_level "minimal" = off
_think_style = {}                                  # base_url -> working style

def _think_fields(style):
    t = cfg["think"]
    if style == "reasoning":
        return {"reasoning": {"enabled": False} if t == "off" else {"effort": t, "exclude": True}}
    if style == "effort":
        return {"reasoning_effort": "none" if t == "off" else t}
    if style == "google":
        level = "minimal" if t == "off" else "high"
        return {"extra_body": {"google": {"thinking_config": {"thinking_level": level, "include_thoughts": False}}}}
    if style == "effort_min":
        return {"reasoning_effort": "minimal" if t == "off" else t}
    if style == "thinking":
        return {"thinking": {"type": "disabled" if t == "off" else "enabled"}}
    return {}

_no_system = set()                                # models that refused the system role

def _fold_system(messages):
    """Some models (older Gemma) reject the system role: put those instructions into the first user turn."""
    if not any(m.get("role") == "system" for m in messages):
        return messages
    sys_text = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str))
    rest = [dict(m) for m in messages if m.get("role") != "system"]
    for m in rest:
        if m.get("role") == "user":
            if isinstance(m.get("content"), str):
                m["content"] = f"[Instructions]\n{sys_text}\n\n[Message]\n{m['content']}"
            else:
                m["content"] = [{"type": "text", "text": f"[Instructions]\n{sys_text}"}] + list(m.get("content") or [])
            break
    else:
        rest.insert(0, {"role": "user", "content": sys_text})
    return rest

# Set by ask(): fired as soon as a model starts sending its answer (streaming), so a model that is
# already answering is never dropped for being "slow" — only one that hasn't started yet.
STARTED = contextvars.ContextVar("__bot_slug___started", default=None)

async def _read_stream(r):
    started = STARTED.get()
    parts, fin = [], None
    async for raw in r.content:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue                               # keep-alive comments (": OPENROUTER PROCESSING")
        body = line[5:].strip()
        if body == "[DONE]":
            break
        try:
            obj = json.loads(body)
        except ValueError:
            continue
        if isinstance(obj, list):
            obj = obj[0] if obj and isinstance(obj[0], dict) else {}
        if obj.get("error"):
            err = obj["error"]
            raise APIError(502, (err.get("message") if isinstance(err, dict) else str(err))[:300])
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            text = delta.get("content") or ""
            # only real words count as "started": Google sends an empty first chunk right away
            if started is not None and (text.strip() or delta.get("reasoning") or delta.get("reasoning_content")):
                started.set()
            if text:
                parts.append(text)
            if ch.get("finish_reason"):
                fin = ch["finish_reason"]
    return "".join(parts), fin

async def _call(model, messages, max_tokens, style, temperature=None):
    base, key, mid, is_or = provider(model)
    if model in _no_system:
        messages = _fold_system(messages)
    payload = {"model": mid, "messages": messages, "max_tokens": max_tokens,
               "temperature": cfg["temperature"] if temperature is None else temperature}
    payload.update(_think_fields(style))
    payload["stream"] = True
    if not key:
        raise APIError(401, f"no API key for {'OpenRouter' if is_or else base}")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if is_or:
        headers["X-Title"] = "__BOT_NAME__ Bot"
    # free queues (numes.ai) can take 40-100 s before answering, so allow longer than the default 90 s
    wait = aiohttp.ClientTimeout(total=90 if is_or else float(env("AI_TIMEOUT", "150") or 150))
    async with HTTP["s"].post(f"{base}/chat/completions", json=payload, headers=headers, timeout=wait) as r:
        if r.status == 200 and "event-stream" in (r.headers.get("Content-Type") or ""):
            text, fin = await _read_stream(r)
            return clean(text), fin
        try:
            data = await r.json(content_type=None)
        except Exception:
            data = {"error": {"message": (await r.text())[:200]}}
        if isinstance(data, list):                 # Google sends errors as [{"error": {...}}]
            data = data[0] if data and isinstance(data[0], dict) else {"error": {"message": str(data)[:300]}}
        if not isinstance(data, dict):
            data = {"error": {"message": str(data)[:300]}}
        if r.status != 200 or not data.get("choices"):
            err = (data.get("error") or {}).get("message", str(data))[:300]
            if r.status == 400 and re.search(r"system|developer instruction", err, re.I) \
                    and any(m.get("role") == "system" for m in messages):
                _no_system.add(model)
                return await _call(model, _fold_system(messages), max_tokens, style, temperature)
            raise APIError(r.status, err)
        ch = data["choices"][0]
        return clean(ch["message"].get("content") or ""), ch.get("finish_reason")

async def _ask_one(model, messages, max_tokens, temperature=None):
    base, _, _, is_or = provider(model)
    if is_or:
        styles = ["reasoning", None]
    elif base in _think_style:
        styles = [_think_style[base]]
    else:
        styles = GOOGLE_STYLES if "googleapis.com" in base else THINK_STYLES
    err = None
    for style in styles:
        try:
            text, fin = await _call(model, messages, max_tokens, style, temperature)
        except APIError as e:
            if e.status in (400, 422) and style is not None:   # provider rejected this field: try the next way
                err = e
                continue
            raise
        if not is_or and base not in _think_style:
            _think_style[base] = style
            log.info("thinking control for %s: %s", base, style or "not supported")
        break
    else:
        raise err
    if not text and fin == "length":
        text, fin = await _call(model, messages, min(max_tokens * 4, 3000), style, temperature)
    if not text:
        raise APIError(0, f"empty reply (finish={fin})")
    return text

# ---------- model health: keep switching cheap ----------
# A model that just failed is skipped for a while, so the next messages go straight to one that works
# instead of paying for the same failure again. Each model's usual "time to first word" is remembered,
# so a normally-fast model that goes quiet is given up on in seconds, not after a fixed long wait.
PAUSE = {"busy": 45, "silent": 300, "error": 120, "gone": 3600}  # seconds to skip a model after each kind of failure
_failed = {}                                       # model -> (failed_at, pause)
_ttft = {}                                         # model -> typical seconds until the first word

def _skip(model):
    f = _failed.get(model)
    return bool(f) and time.monotonic() - f[0] < f[1]

def _mark(model, kind):
    _failed[model] = (time.monotonic(), PAUSE[kind])

def _patience(model):
    """How long to wait for this model's first word before trying the next one."""
    cap = float(cfg.get("hedge", 20) or 0)
    if not cap:
        return 0
    t = _ttft.get(model)
    return min(cap, max(8.0, t * 4)) if t else min(cap, 12.0)

async def ask(messages, max_tokens=None, models=None, temperature=None):
    """Try models in order (default: main + fallbacks). Returns (text, model_used). Output is script-normalized.
    One model at a time; an answer that has started is never cut. Healthy models are tried first."""
    order = list(dict.fromkeys(models or [cfg["model"], *cfg["fallbacks"]]))
    mods = [m for m in order if not _skip(m)] or order[:1]
    errors = []
    for i, m in enumerate(mods):
        last = i == len(mods) - 1
        wait = 0 if last else _patience(m)
        t0 = time.monotonic()
        started = asyncio.Event()
        token = STARTED.set(started)
        task = asyncio.ensure_future(_ask_one(m, messages, max_tokens or cfg["max_tokens"], temperature))
        STARTED.reset(token)
        try:
            waiter = asyncio.ensure_future(started.wait())
            await asyncio.wait({task, waiter}, timeout=wait or None, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            if not task.done() and not started.is_set():
                task.cancel()
                _mark(m, "silent")
                log.info("%s silent for %.0fs, switching to %s", m, wait, mods[i + 1])
                errors.append(f"`{m}` → no answer in {wait:.0f}s")
                continue
            first = time.monotonic() - t0
            try:
                text = await task
            except APIError as e:
                # "high demand" spikes (Google 503) often clear in a second: one quick retry
                if e.status not in (503, 529) or "no provider" in str(e).lower():
                    raise
                await asyncio.sleep(1.0)
                text = await _ask_one(m, messages, max_tokens or cfg["max_tokens"], temperature)
            old = _ttft.get(m)
            _ttft[m] = first if old is None else old * 0.6 + first * 0.4
            log.info("reply from %s in %.1fs (first word %.1fs)", m, time.monotonic() - t0, first)
            return fix_script(text), m
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as e:
            st = e.status if isinstance(e, APIError) else 0
            _mark(m, "busy" if st in (429, 503, 529) else "gone" if st == 404 else "error")
            log.warning("%s failed after %.1fs: %s: %s", m, time.monotonic() - t0, type(e).__name__, str(e)[:200])
            errors.append(f"`{m}` → {type(e).__name__}: {str(e)[:150]}")
    raise RuntimeError("\n".join(errors) or "no models configured")

def friendly_error(e, is_owner):
    if is_owner:
        return f"⚠️ All models failed:\n{e}"[:1990]
    if "per-day" in str(e) or "429" in str(e):
        return "⚠️ I'm a bit busy right now (free limit hit). Try again later!"
    return "⚠️ I'm having trouble right now, try again soon."


# =====================================================================
# output quality guards
# =====================================================================
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
BENGALI = re.compile(r"[\u0980-\u09FF]")
_NO_BN = {0x0904, 0x090D, 0x090E, 0x0911, 0x0912, 0x0929, 0x0931, 0x0933, 0x0934, 0x0945, 0x0946, 0x0949, 0x094A}

def fix_script(text):
    """Weak models sometimes mix Hindi (Devanagari) letters into Bangla. The two Unicode blocks share a layout,
    so shift stray Devanagari characters to their Bengali twins when the text is mostly Bangla."""
    if not text or not DEVANAGARI.search(text):
        return text
    if len(BENGALI.findall(text)) < len(DEVANAGARI.findall(text)):
        return text                                      # real Hindi text: leave it
    out = []
    for ch in text:
        o = ord(ch)
        if 0x0900 <= o <= 0x097F:
            if o in (0x0964, 0x0965):                    # danda / double danda are shared
                out.append(ch); continue
            if o in _NO_BN:                              # letters Bengali doesn't have: use the closest
                ch = {0x0945: "\u09C7", 0x0946: "\u09C7", 0x0949: "\u09CB", 0x094A: "\u09CB",
                      0x0912: "\u0993", 0x0911: "\u0993", 0x090E: "\u098F", 0x090D: "\u098F"}.get(o, "")
                out.append(ch); continue
            out.append(chr(o + 0x80))
        else:
            out.append(ch)
    return "".join(out)

IDENTITY_LEAK = re.compile(
    r"(\bi(?:'m| am)\s+(?:an?\s+)?(?:ai\s+)?(?:language\s+model|llm|chatgpt|claude|gemini|llama|nemotron|deepseek|qwen|gpt)"
    r"|(?:trained|developed|created|made|built|researched)\s+by\s+(?:nvidia|openai|anthropic|google|meta|deepseek|alibaba|mistral|microsoft)"
    r"|(?:nvidia|openai|anthropic|google|meta|deepseek|alibaba)[\s\-]*(?:এর|er|r)?\s*(?:তৈরি|দ্বারা|বানানো|গবেষক|শোধকর্তা|banano|toiri)"
    r"|ভাষা\s*মডেল|language model (?:made|trained|developed))", re.I)

def leaks_identity(text, source=""):
    """True if the reply claims another creator/model (and the user didn't bring it up)."""
    m = IDENTITY_LEAK.search(text or "")
    return bool(m) and not IDENTITY_LEAK.search(source or "")
