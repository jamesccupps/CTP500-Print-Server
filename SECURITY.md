# Security policy

The print server runs on your home network and keeps your photos, so security reports are taken seriously. This policy covers the server and its web app, the PC helper, and the camera and print-box firmware in this repository.

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's private vulnerability reporting instead:
**Security → Report a vulnerability** on this repository
(<https://github.com/jamesccupps/CTP500-Print-Server/security/advisories/new>). Only the maintainer sees it.

Include:
- what an attacker could do with it, and from where (another web site, your network, Bluetooth range);
- the steps to reproduce;
- the version: the release, or `git -C ~/ctp500 log -1 --oneline` on the Pi.

You should hear back within a week. Fixes are credited in the release notes unless you'd rather not be named.

## What counts

- **Another web site using the server.** The server answers only to its own addresses and local names, plus any you add with `allowed_hosts` (see *Lock it down* in the [README](README.md)). A page that gets past that check, by DNS rebinding or otherwise, is in scope.
- **Getting past a token.** With a token set: anything that prints, shows photos or changes settings without it. Token or not: reaching the PC helper's endpoints (`/api/helper/*`) without the helper token.
- **A 3D print file that gets past the server's check.** The server checks every ready-to-print file from the PC helper before anyone gets it: start and end markers, no relative moves, every printing move on the A1 mini's bed. A file that passes and would still drive the printer off its bed is in scope, and it's a physical-safety issue.
- **Getting past the size limits** to tie up a printer, run out its paper or exhaust the Pi's memory.
- **Files the server shouldn't touch:** reading or writing outside its own folders, or loading a font file over HTTP (font files work only from the command line).
- **The token leaking** into a log, a page, or `config.json` when it was given by environment variable.

## What doesn't

- **No token by default.** Out of the box, anyone on your network can print and see the photos. The README says so and shows how to set a token.
- **Plain HTTP.** The server doesn't do TLS. To use it away from home, use Tailscale or another encrypted link.
- **The printers' own Bluetooth.** Who can connect to a CTP500 or an M02 Pro is up to the printer, not the server.

## Supported versions

The latest release and `main`. To update: `cd ~/ctp500 && git pull && ./pi/install.sh`.
