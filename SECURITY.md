# Security

## Reporting a vulnerability

Please report security vulnerabilities privately, never in a public issue, pull request or discussion: use **[Security → Report a vulnerability](https://github.com/SRipaldi/FPP-Backup/security/advisories/new)** (GitHub private vulnerability reporting). Only the maintainer can see the report.

Include what you found, the version or commit, how to reproduce it, and what an attacker could do with it. A proof of concept helps but isn't required.

## What happens next

| Step | Timeline |
|---|---|
| Acknowledgement that the report arrived | within 7 days |
| Assessment: confirmed or not, and severity (CVSS) | within 14 days |
| Fix released, for a confirmed vulnerability | critical or high: within 30 days; medium or low: within 90 days |
| Public disclosure | when the fix is released, or after 90 days, whichever is first, coordinated with you |

You'll get updates in the private advisory as it progresses. Fixes are published as a new release with a GitHub security advisory (and a CVE where one applies), crediting you unless you'd rather not be named. This is a one-person project, so if a deadline slips you'll hear why rather than silence.

Good-faith research is welcome: test against your own installation, don't access other people's controllers or data, and give us the chance to fix before disclosing. We won't pursue or support legal action against research done that way.

## Out of scope

- Vulnerabilities in FPP, xLights or the FPP firmware itself: report those to [FPP](https://github.com/FalconChristmas/fpp/security) or [xLights](https://github.com/xLightsSequencer/xLights) directly.
- Running the web UI on a network interface other than 127.0.0.1, or turning on xLights' automation port and leaving it on, against the guidance below.

## What this tool trusts, and what it doesn't

- **The web UI is for this computer only.** It listens on 127.0.0.1, and every API request needs the per-run token the page is served with. Don't expose it on a network.
- **Controllers are not trusted.** Anything a device on the show network reports (hostnames, file names, settings) is validated before it is used as a path or shown in the UI.
- **Backups contain secrets.** Snapshots hold Wi-Fi keys, OS passwords and `.htpasswd` files; they are written readable by your user only and are gitignored. Built card images contain them too: never publish a `.img`.
- **FPP UI passwords** are kept in the macOS Keychain (service `FPP-Backup`), never on disk.
- **xLights' automation port** (49913) has no authentication. Turn it on only while pushing sequences.

## How the code and releases are checked

- **Code:** CodeQL (security-extended) on every pull request and weekly; merging is blocked on high or critical findings. The shell scripts and Dockerfile are checked by ShellCheck and hadolint.
- **Dependencies:** OSV-Scanner checks the card-builder container weekly. The Python code uses only the standard library and the UI has no JavaScript libraries, so there is nothing else third-party to scan. Secret scanning with push protection is on.
- **Repository:** `main` takes only reviewed pull requests with signed commits; release tags can't be moved or deleted; releases are immutable once published and carry signed SBOMs (`gh attestation verify <sbom> -R SRipaldi/FPP-Backup`). OpenSSF Scorecard reviews the setup weekly.
- **CI itself:** no third-party GitHub Actions. Scanners are pinned by version and SHA-256, and run with read-only tokens and no secrets. AGENTS.md ("CI and scanning") has the details.

## Supported versions

Security fixes go into the latest release and `main`. Older releases are not patched: upgrade to the latest.
