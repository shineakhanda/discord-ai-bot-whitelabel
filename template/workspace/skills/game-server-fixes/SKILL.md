---
name: game-server-fixes
description: Diagnose common game server startup errors from console logs (Minecraft Java/Bedrock, Rust, FiveM, generic) and the exact fix for each.
---
# Reading logs → fix
Look at the LAST error lines in `panel_logs`. Common causes:

| Log says | Cause | Fix |
|---|---|---|
| `You need to agree to the EULA` | EULA not accepted | `panel_write | server | eula.txt | eula=true` → start |
| `UnsupportedClassVersionError`, `class file version 65.0` | Java too old | `panel_image` → Java 21 (65=Java 21, 61=Java 17, 52=Java 8) |
| `requires Java 17` / `Java 8` for old modpacks | Java too new/old | pick the matching Java image |
| `Unable to access jarfile` / `Error: Invalid or corrupt jarfile` | jar missing or wrong name | `panel_startup`: check SERVER_JARFILE → fix var, or `panel_reinstall` |
| `FAILED TO BIND TO PORT`, `Address already in use` | port taken | make sure only one instance runs; restart; ask admin to check allocation |
| `OutOfMemoryError`, container killed, `exit code 137` | not enough RAM | lower view-distance/plugins, or `panel_build` more memory if admin wants |
| `Could not load 'plugins/X.jar'`, `Error occurred while enabling X` | broken plugin | rename the jar to `.disabled`, start, tell admin which plugin |
| `Mod X requires Y` / `Missing mods` (Forge/Fabric) | missing dependency or wrong loader/version | tell admin exact mod + version needed; check egg/loader |
| `Exception in server tick loop` + world/chunk errors | corrupt world/chunk | stop; tell admin; restore backup if they want |
| `Done (x.xxs)! For help, type "help"` | server started fine | report success |
| Install log errors / `curl` / `404` during reinstall | bad version/build value | fix the version variable (exact version that exists) and reinstall |

Rules: never delete worlds or files. Prefer renaming. After a fix, start and confirm with `panel_logs`.
