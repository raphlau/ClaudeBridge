# Claude Bridge

Lets Claude, from any device (phone included), reach two folders on your Windows PC through a custom connector:

- **Files**: your documents (papers, scans, letters, office files...)
- **Notes**: your Markdown notes

It can also use **Chrome on the PC**, and start or close Chrome and the Claude desktop app. It's a small background server, published to the internet with **Tailscale Funnel** and protected by a secret token. It does nothing while nobody uses it, and it updates itself.

## Install

You need Windows, Tailscale (installed, signed in, MagicDNS on), and a Claude account.

1. Open **PowerShell from the Start menu**, not a terminal inside the Claude app (the installer refuses to run there). No admin needed.

   ```powershell
   irm https://github.com/raphlau/ClaudeBridge/releases/latest/download/get.ps1 | iex
   ```

2. Pick your two folders. The installer suggests likely ones (synced document folders, your Obsidian vaults): press Enter for the first, type a number, or paste a path.
3. If Tailscale asks you to approve **Funnel** for this PC, approve it.

At the end it shows the connector details, also saved in `%LOCALAPPDATA%\ClaudeBridge\connector.txt`. Re-running the command keeps your folders, token and settings.

## Add it to Claude

**Settings > Connectors > Add custom connector**:

- **URL:** `https://<your-pc>.<tailnet>.ts.net/k/<token>/mcp`, the first URL in `connector.txt`. It contains the secret, and each PC has its own. Without the `/k/<token>` part, Claude tries an OAuth sign-in and reports that it can't connect.
- **Header instead:** if a client lets you set request headers, `https://<your-pc>.<tailnet>.ts.net/mcp` with `Authorization: Bearer <token>` also works.

In the connector's tool permissions, consider "ask before running" for the tools that change things: `write_text`, `edit_text`, `append_text`, `create_upload_link`, `start_app`, `quit_app`, and the Chrome tools that click, type, navigate or run scripts (`computer`, `form_input`, `navigate`, `javascript_tool`, `browser_batch`).

**Download and upload links** (for big or binary files) need your PC's `*.ts.net` name in Claude's network allowlist.

## What keeps it safe

- **The token:** 256 bits from Windows' cryptographic random generator, made on your PC by the installer. It's stored in `config.json`, readable only by you. It's checked on every request, in constant time.
- **Nothing to see without the token:** a request without the right token, or to a path that doesn't exist, gets no answer at all. The bridge just closes the connection. Addresses that keep trying get ignored, and repeated wrong tokens lock the bridge and alert you (see [Blocking, lockout and alerts](#blocking-lockout-and-alerts)).
- **Only your two folders:** there are no commands or shell. Paths outside the folders, `..`, symlink tricks and odd Windows names (`:` streams, `CON`, trailing dots) are refused.
- **Nothing is deleted or moved,** and before any change the previous version is copied to `%LOCALAPPDATA%\ClaudeBridge\history` (kept 30 days).
- **No programs:** the bridge never writes programs, scripts, shortcuts or installers (`.exe`, `.bat`, `.ps1`, `.js`, `.lnk`, `.url`...). Uploaded files get the same "downloaded from the internet" mark a browser adds, so Office and SmartScreen stay cautious.
- **Hidden:** `.obsidian` (plugin code and settings), `.git`, `.ssh`, password databases and key files.
- **Chrome:** everything happens inside Chrome, under the Claude in Chrome extension's own site permissions. `file_upload` is blocked, since it could send any file on the PC to a website.
- **Updates come only from this repo's releases,** checked against their SHA-256. An update never turns a switched-off bridge back on.
- **Everything is logged** in `logs\bridge.log`, without the text Claude types or writes.

What remains: anyone with the token can do what Claude can. That includes acting in Chrome with your signed-in accounts, so treat the token like a password. Anyone who can publish a release here (your GitHub account) can update the code on your PC, so keep 2FA on.

## Blocking, lockout and alerts

**Without the token:** the bridge sends no HTTP answer at all; it closes the connection. This applies to every path, including `/healthz` (which answers only on this PC, on your tailnet, or with the token) and paths that don't exist. Tailscale's relay still completes the HTTPS handshake, and turns the closed connection into its own empty `502`, the same as when the bridge is switched off.

**Blocking addresses:** an internet address that sends 5 requests without a token, or 3 with a wrong one, within 10 minutes is ignored for 24 hours, and its attempts stop counting toward the lockout below. So a single attacker can't lock you out. It never blocks:
- this PC or your tailnet;
- Claude's OAuth checks (`/.well-known/...`);
- an address that has shown the right token. The right token always gets through a block.

**Locking:** after 10 wrong tokens within 10 minutes (connector or download/upload links) from addresses not yet blocked, the bridge refuses **everything**, the right token included, for 60 minutes.

**Alerts:** it shows a notification on the PC. For a push to your phone as well, install the [ntfy](https://ntfy.sh) app, subscribe to a long random topic name, and add it to `config.json`:

```json
"security": { "alert_url": "https://ntfy.sh/<your-random-topic>" }
```

**Ending it early:** `bridge.cmd unlock`. If the wrong tokens weren't yours, also run `bridge.cmd rotate-token` and update the connector in Claude.

**Settings** (under `"security"`): `block_after_no_token`, `block_after_wrong_token`, `block_hours`, `max_bad_tokens`, `window_minutes`, `lockout_minutes`. `bridge.cmd unlock` also clears blocked addresses.

## What Funnel exposes

**Only one thing:** HTTPS on port 443 of this PC's `*.ts.net` name, forwarded to the bridge on `127.0.0.1:8765`. The installer sets it up (`tailscale funnel --bg --https=443 http://127.0.0.1:8765`), and Tailscale keeps the setting on the PC. See it with:

```powershell
tailscale funnel status
tailscale serve status --json
```

**Nothing else is reachable from the internet:**
- Tailscale opens no ports on your router: traffic comes in through Tailscale's relays over the PC's own outbound connection.
- Funnel can only use ports 443, 8443 and 10000; the other two aren't set up.
- The bridge itself listens on `127.0.0.1` only.

Separately, devices on your **tailnet** (not the internet) can reach the PC like a local network, under your Tailscale access rules. `bridge.cmd off` removes the Funnel.

**Dependencies:** they're pinned in `uv.lock`. Every release, and a weekly job, check them against public vulnerability advisories. A failed weekly run means GitHub emails you; then update the lock and release.

## Chrome on the PC

The bridge runs the Claude in Chrome server that comes with **Claude Code**, and passes its browser tools through. So any chat with the connector can use Chrome here, including one on your phone, without the Claude desktop app.

- **Claude Code:** found automatically: a standalone install, or the copy inside the Claude desktop app, which it follows across app updates.
- **Sign in once:** in PowerShell from the Start menu, run that `claude.exe` and type `/login`, using the same account as the Chrome extension. For the app's copy, the path is `%LOCALAPPDATA%\Claude-Data\claude_*\claude-code\<version>\<build>\claude.exe`.
- **On demand:** the Chrome server starts on the first Chrome call (a few seconds) and stops after 10 minutes without one.
- **Which browser:** the one in use, or the one used last here, or the one Claude Code knows is on this PC. It never picks a browser on another computer by itself. Then Claude asks you, and the answer is remembered.
- **Site permissions:** the extension's own settings apply. If a site isn't allowed, the extension asks in Chrome on the PC, so allow the sites you use in the extension beforehand.

**Settings** (under `"chrome"`): `enabled`, `idle_minutes`, `device_id`, `permission_mode`, `call_timeout_seconds`, `claude_path`.

If something's wrong, ask Claude to run `chrome_status`, or look at `logs\chrome-mcp.log`.

## Updates

**Daily:** a `ClaudeBridge Update` task checks once a day, around 04:00 to 06:00. When there's a newer release, it downloads and verifies it and installs it in place, keeping your settings. If the new version doesn't start, it puts the old one back.

**Now:** ask Claude to "update the bridge" (`update_bridge`), or run `bridge.cmd update`. To turn off the daily check, set `"update": {"auto": false}`.

## Commands

Run these in `%LOCALAPPDATA%\ClaudeBridge`:

| `bridge.cmd ...` | |
|---|---|
| `status` | version, lockout, blocked addresses, last update, and whether it answers locally and from the internet |
| `off` / `on` | kill switch: stop the server and take it off the internet, and back |
| `restart` | after editing `config.json` |
| `logs` | last 60 log lines |
| `update` | install the latest release now |
| `unlock` | end a lockout and clear blocked addresses early |
| `rotate-token` | new secret; update the connector in Claude afterwards |
| `test` | run in the foreground to see startup errors |
| `uninstall` | remove the tasks and the Funnel, then delete the folder yourself |

From your phone, you can also cut it off by disabling this machine in the Tailscale admin console.

## Good to know

- **Sync apps:** if Files is synced by MEGA or Notes is Obsidian, the bridge starts that app when Claude changes something there, so the change uploads. It doesn't touch them otherwise.
- **Restoring a file:** copies are in `history\<Files|Notes>\...\<file>.<date-time>`, or use your sync app's version history.
- **Keeping it reachable:**
  - **Sleep:** set sleep to Never when plugged in, and closing the lid to "Do nothing".
  - **Windows updates:** turn on *Sign-in options > "Use my sign-in info to automatically finish setting up after an update"*, so the bridge comes back after an update restart.
- **Changing folders:** re-run the install command with `-Reconfigure`. It keeps the token:
  ```powershell
  & ([scriptblock]::Create((irm https://github.com/raphlau/ClaudeBridge/releases/latest/download/get.ps1))) -Reconfigure
  ```
- **Checking it end to end:** run `.venv\Scripts\python.exe selftest.py --public`.

## Making a release

1. Bump `VERSION` in `server.py` and commit.
2. Push a matching tag: `git tag v1.6.1`, then `git push origin main v1.6.1`.

The `Release` workflow checks the tag against `VERSION`, checks that the scripts are ASCII and that the Python compiles, then publishes `ClaudeBridge.zip`, its SHA-256, `version.txt` and `get.ps1`. PCs pick it up within a day.
