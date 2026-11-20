---
name: minecraft-smp
description: Minecraft SMP helper — joining, versions, plugins/commands, connection fixes, griefing reports, server status.
---
# Minecraft SMP playbook
## Joining
- IP, port, version, Java/Bedrock support: ONLY from this server's info. If missing, ask staff to add it with
  `/setup knowledge`.
- Java: Multiplayer → Add Server → IP. Bedrock (if Geyser): Servers tab → Add → IP + port (often 19132).
- Common errors:
  - "Outdated client/server" → use the server's version (Java: Installations → New → pick version).
  - "Not whitelisted" → staff must add them (`whitelist add <name>`), ask them to wait/ping staff.
  - "Connection timed out / refused" → check IP spelling, server online? (use panel status if linked), their
    internet; Bedrock needs the right port.
  - "Failed to verify username" → cracked/offline accounts can't join online-mode servers; restart launcher.
  - Lag/rubber-banding → server TPS (staff `/tps`), their ping, too many entities/farms nearby.

## Gameplay help (generic plugin commands — say "usually" if unsure the server uses them)
- Homes/teleport: `/sethome`, `/home`, `/tpa <player>`, `/tpaccept`, `/spawn`, `/back` (EssentialsX).
- Land claims (GriefPrevention): golden shovel to claim, `/trust <player>`, `/abandonclaim`. Lands/Towny differ.
- Economy: `/bal`, `/pay`, `/shop` or `/ah` (depends on plugins).
- Farms/redstone/building: give practical, version-correct tips.

## Rule breaks
- Griefing, x-ray, hacking accusations → don't judge. Ask for coordinates, time, screenshots, and point to staff /
  a report ticket. Coreprotect (`/co inspect`, `/co rollback`) is for staff.

## Server status & fixes (if a game panel is linked)
- Check status/console with panel tools; for crashes load skill `game-server-fixes`.
