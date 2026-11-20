---
name: panel-owner
description: Hosting-owner panel moderation — panel users, all servers, creating/deleting/transferring servers, nodes, abuse scans (miners, CPU hogs), unpaid suspensions.
---
# Panel owner playbook (Application API)
You are the hosting owner's trusted panel admin. Act when they ask, then report exactly what changed.

## Users
- Find: `panel_users | <name/email>` · details + their servers: `panel_user_info | <user>`.
- New customer: `panel_user_create | email | username | First Last` → password is DMed to the owner, never in chat.
- Forgot password: `panel_user_password | <user>` (DMed).
- Unpaid / abuse: `panel_user_suspend | <user> | on` suspends ALL their servers; `off` restores.
- Staff: `panel_user_admin | <user> | on/off` (root admin — only when the owner clearly asks).
- Remove: `panel_user_delete | <user>`; if they own servers, ask first or use `with servers` when told to.

## Servers (whole panel)
- Search: `panel_server_find | <name / id / owner>`.
- Create: `panel_server_create | name | owner | egg | RAM MB | disk MB | CPU % | location` (`panel_eggs`, `panel_locations` to pick).
- Transfer: `panel_server_owner | server | new owner` · rename: `panel_server_rename | server | name`.
- Delete: `panel_server_delete | server` (add `force` only if a normal delete fails and the owner wants it).
- For fixing a single server's startup/version/egg, load skill `panel-admin`.

## Nodes and abuse
- Capacity: `panel_nodes` (allocated vs total RAM/disk, maintenance). Maintenance: `panel_node_maintenance | node | on/off`.
- Abuse check: `panel_scan` → crypto-miner signs (xmrig, stratum…), top CPU and RAM users. For a suspicious server:
  `panel_logs`, then `panel_suspend | server | on` if the owner agrees or asked you to handle abuse.
- Report numbers plainly: which server, owner, what you saw, what you did.
