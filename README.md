# White-label Discord AI bot

> Made by Shine Akhanda · shineakhanda.dev

A full AI member for Discord servers — chat in Bangla/Banglish/English, web search & page reading, data
collection to CSV, page watching, tickets, AI moderation, announcements, activities (QOTD, trivia, games,
giveaways, polls), game-panel admin, VPS help over SSH — with **your own name and personality**.

## Make your own bot
1. Create a Discord application + bot at https://discord.com/developers (enable **Message Content Intent**).
2. Edit **`brand.json`**:
   | key | what |
   |---|---|
   | `name` | the bot's name (e.g. `Nova`) |
   | `owner` | who owns it (shown in its identity) |
   | `triggers` | words that call it in chat, e.g. `["nova", "নোভা"]` |
   | `vibe`, `style` | personality, one line each |
   | `color` | embed colour, e.g. `#7C3AED` |
   | `website`, `stream_url` | status texts and streaming link |
3. Copy `.env.example` to `.env` and fill `DISCORD_TOKEN`, `OWNER_ID` and one AI key
   (`GOOGLE_AI_KEY` for free Gemma, `OPENROUTER_API_KEY`, or `NUMES_API_KEY`).
4. Run **`python start.py`** (Pterodactyl: set the start file / `PY_FILE` to `start.py`).

`start.py` renders `template/` with your brand into `app/` and starts it. All data lives in `data/` and is never
touched by updates. Change `brand.json` and restart to rebrand.

Deeper personality: edit `template/workspace/SOUL.md` and `IDENTITY.md` (`__BOT_NAME__`/`__OWNER__` are filled in
automatically).

## Licence note
The footer **"Made by Shine Akhanda · shineakhanda.dev"** must stay. `start.py` refuses to run if it is removed from `template/core.py`.
