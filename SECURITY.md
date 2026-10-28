# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use **Security → Report a vulnerability** on this repository (GitHub private vulnerability reporting). Include what you found, how to reproduce it and what an attacker could do with it. You'll get a reply as soon as I can look at it; this is a one-person hobby project, so allow a few days.

## What this tool trusts, and what it doesn't

- **The web UI is for this computer only.** It listens on 127.0.0.1, and every API request needs the per-run token the page is served with. Don't expose it on a network.
- **Controllers are not trusted.** Anything a device on the show network reports (hostnames, file names, settings) is validated before it is used as a path or shown in the UI.
- **Backups contain secrets.** Snapshots hold Wi-Fi keys, OS passwords and `.htpasswd` files; they are written readable by your user only and are gitignored. Built card images contain them too: never publish a `.img`.
- **FPP UI passwords** are kept in the macOS Keychain (service `FPP-Backup`), never on disk.
- **xLights' automation port** (49913) has no authentication. Turn it on only while pushing sequences.

## Supported versions

Only the latest commit on `main`.
