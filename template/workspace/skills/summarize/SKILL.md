---
name: summarize
description: Summarize a link, article, PDF, website, Discord channel or the recent chat — "what's this link about", "tldr", "summarize #announcements", "what did I miss".
---

<!-- Adapted from OpenClaw's bundled "summarize" skill (MIT, © OpenClaw Foundation), for Discord tools. -->

# Summarize

1. Get the real content first — never summarize from memory:
   - a link / website → `read` (JavaScript sites work). Long page → it's already trimmed to what matters.
   - a Discord channel → `channel` with ~50–100 messages.
   - "what did I miss" / this chat → `history`.
2. Write the summary:
   - first line: the one-sentence gist;
   - then 3–6 bullets with the key facts (names, numbers, dates, decisions) exactly as written;
   - for a chat: who wants what, what was decided, what's still open.
3. Same language as the user. If the source couldn't be read, say exactly why (login wall, bot check, empty page).
