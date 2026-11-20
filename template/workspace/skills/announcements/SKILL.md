---
name: announcements
description: Write and post announcements like a real community manager, or schedule them (once, daily, weekly).
---
# Announcements
- Admin asks to announce something → `announce_drafts | #channel | ping | details | when` (when is optional, e.g. "friday 8pm"). __BOT_NAME__ shows 3 drafts with buttons; the admin picks one and it is posted (or scheduled).
  - ping: `none`, `here`, `everyone`, or a role like `@Gamers`. Use what the admin said; default `none`.
- Admin gives exact text ("post this exactly: …") → `announce_post | #channel | ping | text`.
- Repeating/automatic messages → `schedule_add | #channel | when | repeat | ping | text` with repeat `none`, `daily`, `weekly` or `every 6h`.
- See / cancel: `schedule_list`, `schedule_cancel | id`.

## Writing style (drafts)
Human, warm, scannable: a hook line with 1 emoji, the key facts (what, when with timezone, where, prize/how to join), one call-to-action. Server language (Banglish servers → Banglish). No hashtags spam, max 4 emoji.
