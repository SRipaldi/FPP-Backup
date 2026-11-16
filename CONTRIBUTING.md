# Contributing

Thanks for looking. This is a personal project that's open for others to use and improve.

- **Issues** are welcome: bugs, questions, FPP versions or hardware that don't work.
- **Pull requests**: fork the repo, make your change on a branch and open a PR against `main`. Every PR needs the owner's review before it's merged; `main` can't be pushed to directly.
- Keep to the project's constraints: Python standard library only (3.9-compatible), plain JavaScript with no build step, and nothing that writes to a controller without the user asking. [AGENTS.md](AGENTS.md) has the architecture and the checks to run.
- Never include anything from `config/`, `snapshots/` or `work/`: they hold your addresses, passwords and Wi-Fi keys.
- Keep your own show out of everything you publish: commits, commit messages, PR text, code comments and tests. That means no controller names, IP addresses, show or song names, and no secrets or personal information. Write "the player", "a remote", `10.0.0.x`. Run `tools/check_public.py --install-hooks` once; it then checks every commit and push against your own installation's names and addresses.
- Security problems: see [SECURITY.md](SECURITY.md) instead of opening an issue.
