---
name: vps-hardening
description: Security audit and staged hardening of a linked VPS / Linux server (SSH, firewall, updates, open ports, backups) — read-only first, changes only after approval.
---

<!-- Adapted from OpenClaw's bundled "healthcheck" skill (MIT, © OpenClaw Foundation), for SSH-linked servers. -->

# VPS security check & hardening

Goal: assess the server's risk with read-only checks, then propose staged, reversible hardening without breaking
access.

## Rules
- Read-only checks first. Never change SSH, firewall or remote access until the access path is confirmed.
- Every change needs the admin's approval (the SSH tools enforce it). Prefer reversible steps; give a rollback line.
- Never print secrets (keys, passwords, tokens). Choices for the user as a numbered list.

## Context to find first (ask only what's missing)
OS/version · container or full VM · how admins log in (password / key) · public IP or behind a proxy/tunnel ·
what runs on it (panel, game servers, web) · backups · automatic security updates.

## Read-only checks (`ssh_check` templates first, then `ssh_run`)
- `cat /etc/os-release` · `uptime` · `df -h`
- Listening ports: `ss -ltnup`
- Firewall: `ufw status verbose || firewall-cmd --list-all || nft list ruleset | head -50`
- SSH: `sshd -T | egrep 'permitrootlogin|passwordauthentication|port|pubkeyauthentication'`
- Failed logins: `journalctl -u ssh -u sshd --since '24 hours ago' | grep -ci 'failed password'`
- Updates: `apt list --upgradable 2>/dev/null | wc -l` · `systemctl is-enabled unattended-upgrades`
- fail2ban: `systemctl is-active fail2ban`

## Risk level (ask once)
1. Convenience — private, few changes. 2. Balanced — secure defaults. 3. Strict — public/sensitive, lock down.

## Report shape
- **Posture:** one short paragraph.
- **Findings:** severity · evidence · why it matters.
- **Plan:** staged, reversible steps; read-only first, writes only after approval.
- **Couldn't check:** gaps.

## Hardening menu (offer only what's relevant, confirm each)
Enable firewall allowing only needed ports (SSH first!) · disable password SSH / root login once a key works ·
install fail2ban · enable unattended security updates · close unused public ports · verify backups and a restore.
