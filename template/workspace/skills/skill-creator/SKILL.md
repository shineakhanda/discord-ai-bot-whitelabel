---
name: skill-creator
description: Teach the bot a new skill (a reusable playbook) from Discord — when the bot owner says "learn how to…", "make a skill for…", or describes a procedure to follow every time.
---

<!-- Adapted from OpenClaw's bundled "skill-creator" skill (MIT, © OpenClaw Foundation). -->

# Creating a skill

A skill is a short playbook the bot loads before doing that kind of job. Only the bot owner can create one (skills apply in every server the bot is in).

1. Understand the job: when should it be used (trigger), what steps, what tools, what to never do.
   Ask at most one short question if something important is missing.
2. Write it with the `create_skill` tool:
   - `name`: short, lowercase, dashes (e.g. `order-handling`).
   - `description`: ONE line saying what it does AND when to use it — this is what you'll see in the skills list.
   - `body`: Markdown. Title, then numbered steps, the tools to use, rules ("never…", "always…"), and the answer
     format. Keep it under ~60 lines. Use the admin's real details (channels, prices, roles, wording).
3. Confirm in one line: the skill name and when it will be used.
4. To change a skill, create it again with the same name. To remove it, use `delete_skill`.
