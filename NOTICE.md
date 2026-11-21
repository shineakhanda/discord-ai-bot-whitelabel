# Third-party notices

Parts of this bot's agent design and some skills are adapted from **OpenClaw**
(https://github.com/openclaw/openclaw), MIT License, Copyright (c) 2026 OpenClaw Foundation:

- Agent loop with native tool calling, per-conversation queue, conversation compaction and the
  `<available_skills>` catalog are re-implemented in Python following OpenClaw's architecture.
- Skills adapted from OpenClaw's bundled skills: `weather`, `vps-hardening` (from `healthcheck`),
  `skill-creator`.

MIT License text: https://github.com/openclaw/openclaw/blob/main/LICENSE
