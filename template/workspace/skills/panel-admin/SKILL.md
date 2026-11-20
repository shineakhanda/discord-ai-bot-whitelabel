---
name: panel-admin
description: Manage and fix game servers on the linked Pterodactyl / Reviactyl / Pelican panel (status, logs, startup, reinstall, version or egg change, files, limits).
---
# Panel admin playbook
You have admin access to this Discord server's game panel. Admins asked for fully automatic fixes: act, then report.

## Always
1. Find the server: `panel_list` if unsure of the name.
2. Check before changing: `panel_status` then `panel_logs`.
3. Make the smallest fix that solves it. One change at a time, then verify with `panel_power | <server> | start` + `panel_logs`.
4. Report: what was wrong (1 line), what you changed, current state.
5. Reinstall and egg change wipe/replace server files. A backup is made automatically when the server has a free backup slot; say whether it was made.

## Tasks
- **Won't start / crashing** → load skill `game-server-fixes`, read `panel_logs`, fix the cause.
- **Change version** (e.g. Minecraft 1.20.4 → 1.21.1): `panel_startup` → find the version variable (MINECRAFT_VERSION, VERSION, MC_VERSION, BUILD_NUMBER, VANILLA_VERSION…) → `panel_set_var` → `panel_reinstall` → start → check logs. If the new version needs newer Java, also `panel_image` to a matching Java image from the list in `panel_startup`.
- **Change egg / server type** (Paper → Fabric, Vanilla → Forge…): `panel_eggs` to find the egg → `panel_change_egg | <server> | <egg>` (reinstalls) → start → check logs.
- **Change Java / Docker image**: `panel_startup` shows allowed images → `panel_image | <server> | <image or label>`.
- **More RAM / disk / CPU**: `panel_build | <server> | memory=4096 disk=20480 cpu=200` (MB, MB, %). Only when an admin asks.
- **Run a console command**: `panel_command | <server> | <command>` (e.g. `say Restarting in 5 min`, `op Rahim`).
- **Edit a config**: `panel_read` first, then `panel_edit | server | path | old text | new text`. Use `panel_write` only for small new files (eula.txt).
- **Disable a broken plugin/mod**: `panel_rename | server | plugins/Bad.jar | plugins/Bad.jar.disabled`.
- **Suspend / unsuspend**: only when an admin clearly asks.
