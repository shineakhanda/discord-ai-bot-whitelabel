# White-label Discord AI bot

> Made by Shine Akhanda · shineakhanda.dev

A full AI member for Discord servers, under **your own name and personality**:
- chat in English, Bangla and Banglish, with web search and page reading
- web data to CSV, and page watching
- tickets, AI moderation and announcements
- activities: QOTD, trivia, mini-games, giveaways and polls
- game-panel admin, and VPS help over SSH

📘 **Full usage guide (PDF):** [`docs/Bot-Guide.pdf`](docs/Bot-Guide.pdf)
It covers every slash command, owner command, setting and troubleshooting step. It uses the name "Kashem", but your bot works the same way under your own name.

---

## Contents
1. [Create the Discord bot](#1-create-the-discord-bot)
2. [Set your brand](#2-set-your-brand-brandjson)
3. [Fill in `.env`](#3-fill-in-env)
4. [Install: Pterodactyl / Pelican / Reviactyl panel](#4a-install-on-a-pterodactyl--pelican--reviactyl-panel-recommended)
5. [Install: VPS or your own PC](#4b-install-on-a-vps-or-your-own-pc)
6. [Connect the repo to the bot (auto-updates)](#5-connect-the-github-repo-to-the-bot-auto-updates)
7. [First steps in Discord](#6-first-steps-in-discord)
8. [Troubleshooting](#7-troubleshooting)

---

## 1. Create the Discord bot
1. Open <https://discord.com/developers/applications>, click **New Application** and name it.
2. Go to **Bot** and click **Reset Token**. Copy the token; it goes in `.env` as `DISCORD_TOKEN`.
   **Never share it.** If it leaks, reset it.
3. In **Bot → Privileged Gateway Intents**, turn on **Message Content Intent**.
   Turn on **Server Members Intent** too if you want welcome messages.
4. Go to **OAuth2 → URL Generator** and tick the scopes `bot` and `applications.commands`.
   - Permissions: **Administrator**, or at least Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages, Moderate Members, Kick, Ban, Manage Channels.
   - Open the generated link to invite the bot.
5. Your own Discord user ID goes in `OWNER_ID`. To copy it, turn on Settings → Advanced → Developer Mode, then right-click your name → **Copy User ID**.

## 2. Set your brand (`brand.json`)
```json
{
  "name": "Nova",
  "owner": "Your Name",
  "triggers": ["nova"],
  "vibe": "friendly, smart, chill, reliable",
  "style": "Friendly and warm, quick, a little funny, never fake.",
  "color": "#14B8A6",
  "website": "example.com",
  "stream_url": "https://www.twitch.tv/discord"
}
```
| key | what it does |
|---|---|
| `name` | The bot's name. Spaces are fine, e.g. `Cave Cloud`. |
| `owner` | Who owns the bot. It says this when asked. |
| `owner_notes` | Optional extra facts about the owner or business. |
| `triggers` | Words that call the bot in chat, e.g. `["nova", "নোভা"]`. |
| `vibe`, `style` | Personality, one line each. |
| `color` | Embed colour, e.g. `#7C3AED`. |
| `website`, `stream_url` | Used in status texts and the streaming link. |

To rebrand later, change `brand.json` and restart. For deeper personality, edit `template/workspace/SOUL.md` and `IDENTITY.md`.

## 3. Fill in `.env`
Copy `.env.example` to `.env` and fill in:

| key | needed? | where to get it |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Developer Portal → Bot → Reset Token |
| `OWNER_ID` | ✅ | Your Discord user ID (numbers only) |
| `GOOGLE_AI_KEY` | ✅ recommended | <https://aistudio.google.com/apikey>: free Gemma 4 with a high daily limit |
| `OPENROUTER_API_KEY` | optional | <https://openrouter.ai/keys>: free backup models |
| `NUMES_API_KEY` | optional | numes.ai: free DeepSeek / GLM backups |
| `SERPER_API_KEY`, `TAVILY_API_KEY` | optional | Better web search. Without them the bot uses DuckDuckGo. |
| `MEMBERS_INTENT` | optional | `1` turns on welcomes. You also need Server Members Intent on. |

You need at least **one** AI key. If you add more, the bot switches to the next model automatically when one is busy.

## 4a. Install on a Pterodactyl / Pelican / Reviactyl panel (recommended)
1. **Create a server** with the **Python Discord Bot** egg (parkervcp/yolks, Python 3.12 or 3.13).
   - 512 MB RAM is enough to start; 1 GB is comfortable. 2 GB disk.
2. **Startup tab:**

   | variable | value |
   |---|---|
   | Git Repo Address | your repo URL, e.g. `https://github.com/you/discord-ai-bot-whitelabel.git` |
   | Install Branch | `main` |
   | Git Username | your GitHub username |
   | Git Access Token | a GitHub token (see [section 5](#5-connect-the-github-repo-to-the-bot-auto-updates)); needed if the repo is private |
   | Auto Update | `1` |
   | App py file | `start.py` |
   | Requirements file | `requirements.txt` |

3. **Settings → Reinstall Server.** This clones the repo into the server. On an empty server you can also upload the files with the file manager.
4. In **Files**, create `.env` (copy `.env.example`) and edit `brand.json`.
5. **Start.** The first start installs the Python packages, then you'll see:
   ```
   [Nova] rendered from brand.json · Made by Shine Akhanda · shineakhanda.dev
   INFO ...: synced 41 slash commands
   INFO discord.gateway: Shard ID None has connected to Gateway
   ```
6. To update later, push to GitHub and restart the server. With Auto Update set to `1`, it runs `git pull` on every start. Your `.env`, `brand.json` and `data/` are never overwritten.

## 4b. Install on a VPS or your own PC
Python 3.11+ and git are required.
```bash
git clone https://github.com/you/discord-ai-bot-whitelabel.git mybot
cd mybot
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and brand.json
python start.py
```
To keep it running 24/7 on Linux, use a service:
```ini
# /etc/systemd/system/mybot.service
[Unit]
Description=My Discord AI bot
After=network-online.target

[Service]
WorkingDirectory=/root/mybot
ExecStartPre=/usr/bin/git pull --ff-only
ExecStart=/root/mybot/.venv/bin/python start.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload && systemctl enable --now mybot
journalctl -u mybot -f          # live logs
```
To update: `systemctl restart mybot` (it runs `git pull` first).

## 5. Connect the GitHub repo to the bot (auto-updates)
Keep your bot in **your own GitHub repo** and let the server pull it on every restart.

1. **Make your copy:**
   - Option 1: click **Use this template** or **Fork** on GitHub.
   - Option 2: create an empty **private** repo, then push these files to it:
     ```bash
     git remote set-url origin https://github.com/you/your-bot.git
     git push -u origin main
     ```
2. **Create an access token** (only needed for private repos):
   - Go to GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
   - **Repository access:** *Only select repositories*, then pick your bot repo.
   - **Permissions → Repository → Contents:** *Read-only*.
   - Copy the token (it starts with `github_pat_`).
3. **Put it in the panel:** in Startup, set **Git Repo Address**, **Git Username** and **Git Access Token**, and set **Auto Update** to `1`.
   On a VPS, clone using the token or use an SSH deploy key instead.
4. **Test it:**
   - Restart the server.
   - The console should show `git pull` output, e.g. `Already up to date.` or `Fast-forward`.
   - If you see `could not read Username` or `403`, the token is missing, has expired, or doesn't have Contents access to that repo.

> ⚠️ Never commit `.env` (it's already in `.gitignore`), and never paste tokens in Discord.
> If a token leaks, revoke it on GitHub or Discord and make a new one.

### Running several bots from one repo
Every server can use the same repo with a different `brand.json` and `.env`. One push updates all of them on their next restart, and each bot keeps its own `data/`.

## 6. First steps in Discord
| command | why |
|---|---|
| `/setup type` | Pick the server kind: community, support, store, gaming, Minecraft SMP or hosting. |
| `/setup knowledge` | Rules, IP, prices and FAQ, so answers are correct. |
| `/setup language`, `/setup timezone` | Reply language; time for reminders and schedules. |
| `/setup autochannel` | A channel where the bot answers every message. |
| `/setup moderation`, `/setup automod` | AI moderation and raid / link protection. |
| `/activity qotd`, `/activity trivia` | Daily activities. |
| `/help` | Everything the bot can do. |

Owner commands (only `OWNER_ID`): `!ohelp`, `!status`, `!model`, `!fallback`, `!hedge`, `!think`, `!backup` and more. See the PDF guide.

## 7. Troubleshooting
| problem | fix |
|---|---|
| Stuck on "starting", or `could not read Username` | The repo is private and the Git Access Token is empty or wrong. See section 5. |
| `invalid literal for int()` at `OWNER_ID` | `OWNER_ID` must be your numeric user ID, not the bot token. |
| `Improper token has been passed` | Wrong or old `DISCORD_TOKEN`. Reset it in the Developer Portal. |
| Bot is online but ignores messages | Turn on **Message Content Intent**; check channel permissions; mention it or use `/ask`. |
| Slash commands don't show | Wait a minute and restart Discord; re-invite with `applications.commands`; owner can run `!sync`. |
| `All models failed` | Add or check an AI key (`GOOGLE_AI_KEY` is the easiest); run `!status`. |
| `No module named discord` | Run `pip install -r requirements.txt`, or restart once on a panel. |
| `template/core.py was changed` | The watermark was removed from `template/core.py`. Put it back (see the licence note). |

## Licence note
The footer **"Made by Shine Akhanda · shineakhanda.dev"** must stay. `start.py` refuses to run if it is removed from `template/core.py`.
