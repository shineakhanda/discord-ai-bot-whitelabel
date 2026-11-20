"""Server types: what kind of Discord server __BOT_NAME__ is in, and how it should behave there.
Set by admins with /setup type. The 'focus' text goes into __BOT_NAME__'s instructions for that server."""

TYPES = {
    "general": {
        "label": "🌐 General",
        "focus": "",
        "skill": "",
        "setup": ["`/setup knowledge` — tell me about your server", "`/setup type` — pick what kind of server this is"],
    },
    "community": {
        "label": "💬 Community / hangout",
        "focus": ("This is a COMMUNITY server. Be the friendly regular who keeps chat alive: welcome newcomers by "
                  "name, ask follow-up questions, remember people, bring quiet members in, suggest the server's "
                  "activities (trivia, QOTD, games, events) when chat is slow. Never spam or force it."),
        "skill": "community-engagement",
        "setup": ["`/activity qotd` — daily question of the day", "`/activity trivia` — daily trivia + weekly winners",
                  "`/activity game` — counting / word chain / guess the number channels",
                  "`/setup welcome` — AI welcome messages", "`/setup autochannel` — a channel where I chat freely",
                  "`/giveaway start` · `/poll` — giveaways and polls", "`/setup moderation` — AI admin"],
    },
    "support": {
        "label": "🎧 Support / help desk",
        "focus": ("This is a SUPPORT server. Be a calm, professional support agent: understand the problem, ask for "
                  "the one or two details you need (error text, screenshot, order/account ID), give numbered steps, "
                  "confirm it worked. Use the server's info for policies. Never promise refunds, credits or "
                  "deadlines — hand those to staff (open/point to a ticket). Structure answers clearly."),
        "skill": "ticket-support",
        "setup": ["`/setup knowledge` — FAQ, policies, links", "`/setup tickets` — AI first reply in tickets",
                  "`/setup autochannel` — a help channel I answer in", "`/setup moderation` — keep it clean"],
    },
    "store": {
        "label": "🛒 Store / shop",
        "focus": ("This is a STORE server. Act like a helpful shop assistant: answer about products, prices, "
                  "stock, payment methods and delivery ONLY from this server's info — never invent prices, "
                  "discounts or stock. Guide buyers to order the way the server says (ticket, website, DM staff). "
                  "Be polite with complaints and move them to a ticket. Recommend gently; never pressure."),
        "skill": "store",
        "setup": ["`/setup knowledge` — products, prices, payment, delivery, refund policy",
                  "`/setup tickets` — orders & complaints in tickets", "`/watch add` — watch supplier/competitor prices",
                  "`/extract` — pull product lists from websites into CSV", "`/giveaway start` — promo giveaways"],
    },
    "game": {
        "label": "🎮 Gaming community",
        "focus": ("This is a GAMING server. Talk like a gamer friend: games, matches, tips, LFG, events and "
                  "tournaments. Help with game issues step by step. Keep it hype but friendly."),
        "skill": "",
        "setup": ["`/activity trivia` — gaming trivia (`topic: video games`)", "`/giveaway start` — game key giveaways",
                  "`/poll` — vote on next game night", "`/setup panel` — if you host game servers"],
    },
    "minecraft": {
        "label": "⛏️ Minecraft SMP",
        "focus": ("This is a MINECRAFT SMP server. Know Minecraft well: Java vs Bedrock joining, versions, common "
                  "plugins (EssentialsX, LuckPerms, GriefPrevention/land claims, economy, /tpa, /home), farms, "
                  "redstone, mods. Take the IP, version and rules ONLY from this server's info. Help with "
                  "connection problems (wrong version, whitelist, 'outdated client', timeouts). Report griefing/"
                  "cheating to staff instead of judging. If a game panel is linked, you can check/restart the server."),
        "skill": "minecraft-smp",
        "setup": ["`/setup knowledge` — IP, version, Java/Bedrock, rules, store link",
                  "`/setup panel` — let me check/restart the server", "`/activity trivia` with `topic: Minecraft`",
                  "`/activity game` — counting / word chain", "`/giveaway start` — rank/key giveaways"],
    },
    "hosting": {
        "label": "🖥️ Hosting company",
        "focus": ("This is a HOSTING company's server (game servers / VPS). Be a skilled support engineer: "
                  "billing and plans only from this server's info, panel issues with the panel tools, VPS problems "
                  "via a shared session (vps_connect) — check first, explain, fix with approval. Professional tone."),
        "skill": "panel-admin",
        "setup": ["`/setup panel` — panel access for checks/fixes", "`/ssh settings client_help:True` — customers "
                  "share their VPS", "`/setup tickets` — AI first reply in tickets", "`/setup knowledge` — plans & prices"],
    },
}

def focus(kind):
    t = TYPES.get(kind or "general", TYPES["general"])
    if not t["focus"]:
        return ""
    s = f"# This server\n{t['focus']}"
    if t["skill"]:
        s += f"\nFor anything beyond small talk here, load skill `{t['skill']}`."
    return s
