# Claude Bridge

Lets Claude, from any device (phone included), reach two folders on your Windows PC through a custom connector:

- **Files**: your documents (papers, scans, letters, office files...)
- **Notes**: your Markdown notes

It can also use **Chrome on the PC**. It's a small background server, published to the internet with **Tailscale Funnel** and protected by a secret token. It does nothing while nobody uses it, and it updates itself.

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

Then set the tool permissions; [Tools](#tools) suggests one for each.

**Download and upload links** (for big or binary files) need your PC's `*.ts.net` name in Claude's network allowlist.

## Tools

30 tools. "Allow" means it can't change anything (or only shows status); "Ask" means it changes files or acts in your browser.

| Tool | What it does | Suggested |
|---|---|---|
| `list_dir` | list a folder, or the shared folders (secret files are marked) | Allow |
| `find_files` | find files by name | Allow |
| `search_text` | search inside text files | Allow |
| `read_text` | read a text file | Allow |
| `read_pdf` | read a PDF's text | Allow |
| `view_file` | see an image or a PDF page | Allow |
| `create_download_link` | short-lived link to fetch one file into Claude's workspace (for a secret file: one use, 5 minutes) | Allow |
| `write_text` | create a file, or replace one when asked | Ask |
| `edit_text` | replace a passage in a file | Ask |
| `append_text` | add to the end of a file | Ask |
| `make_folder` | create a folder | Ask |
| `delete_files` | delete files or empty folders, when you asked or agreed (each is kept in history first) | Ask |
| `restore_file` | list the copies history kept of a file, or put one back | Ask |
| `create_upload_link` | one-time link to put a file from Claude's workspace onto the PC | Ask |
| `chrome_status` | Chrome link status; picks the browser if the bridge can't tell | Allow |
| `update_bridge` | check for a new version, or install it | Ask |
| `tabs_context_mcp` | list Claude's tabs (can open its own tab group) | Allow |
| `tabs_create_mcp` / `tabs_close_mcp` | open / close a tab in Claude's tab group | Allow |
| `read_page` / `find` / `get_page_text` | read the page | Allow |
| `read_console_messages` / `read_network_requests` | page console and network log | Allow |
| `navigate` | go to a URL | Ask |
| `computer` | click, type, scroll, take screenshots | Ask |
| `form_input` | fill in form fields | Ask |
| `javascript_tool` | run JavaScript in the page | Ask |
| `browser_batch` | several of the above in one go | Ask |

| `file_upload` | attach files from Files or Notes to a page, e.g. email attachments (up to 10 MB in one go) | Ask |

Not offered: `upload_image`, `gif_creator`, `resize_window`, `list_connected_browsers`, `select_browser` and `switch_browser`. The bridge picks the browser itself.

## What keeps it safe

- **The token:** 256 bits from Windows' cryptographic random generator, made on your PC by the installer. It's stored in `config.json`, readable only by you. It's checked on every request, in constant time.
- **Nothing to see without the token:** a request without the right token, or to a path that doesn't exist, gets no answer at all. The bridge just closes the connection. Addresses that keep trying get ignored, and repeated wrong tokens lock the bridge and alert you (see [Blocking, lockout and alerts](#blocking-lockout-and-alerts)).
- **Only your two folders:** there are no commands or shell. Paths outside the folders, `..`, symlink tricks and odd Windows names (`:` streams, `CON`, trailing dots) are refused.
- **Every change and deletion can be undone:** before a file is changed or deleted, the bridge keeps it in `%LOCALAPPDATA%\ClaudeBridge\history` for 30 days, and `restore_file` puts it back. `delete_files` is only for files you asked for or agreed to, or redundant files Claude made itself. It never deletes through links or deletes a folder with anything left in it, and the Claude app asks you before each call (it's marked destructive). Nothing is moved.
- **No programs:** the bridge never writes programs, scripts, shortcuts or installers (`.exe`, `.bat`, `.ps1`, `.js`, `.lnk`, `.url`...). Uploaded files get the same "downloaded from the internet" mark a browser adds, so Office and SmartScreen stay cautious.
- **Hidden:** `.obsidian` (plugin code and settings), `.git`, `.ssh`, password databases and key files.
- **Secret files are link-only:** a file with `.secret` in its name (`synci-token.secret.txt`, `api.secret`) is for tokens, API keys and passwords that Claude's scripts need. It's listed, marked `[secret: link only]`, and Claude can fetch it with a download link, which then works once, for at most 5 minutes, or replace it with an upload link. The bytes go straight between the PC and a file in Claude's workspace. No tool reads, searches, edits or attaches it to a web page, so its contents can't end up in a chat, even by a fallback or a mistake. Every refusal tells Claude the right way to use it instead: save it to a private file, let the script read it from there, never print it. Change the pattern with `secret_files` in `config.json`.
- **Chrome:** everything happens inside Chrome, under the Claude in Chrome extension's own site permissions.
- **Uploads to websites only from Files and Notes:** `file_upload` takes bridge paths, which are checked like any other file tool, so hidden folders, keys and password files are excluded. The bridge then hands Chrome the files' contents. Claude Code itself can't read files on the PC, so nothing else can be uploaded.
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
- **On demand:** on the first Chrome call, the bridge starts Chrome if it isn't running, then the Chrome server (a few seconds). It stops the server after 10 minutes without a Chrome call.
- **Which browser:** the one in use, or the one used last here, or the one Claude Code knows is on this PC. It never picks a browser on another computer by itself. If it can't tell, Claude asks you and calls `chrome_status(browser=...)`, and your answer is remembered.
- **Site permissions:** the extension's own settings apply. If a site isn't allowed, the extension asks in Chrome on the PC, so allow the sites you use in the extension beforehand.

**Settings** (under `"chrome"`): `enabled`, `idle_minutes`, `profile` (a Chrome profile folder such as `"Profile 1"`, if Claude in Chrome is not in the default one), `device_id`, `permission_mode`, `call_timeout_seconds`, `claude_path`.

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

- **Sync apps:** the bridge works out by itself which app syncs each folder: Obsidian if the folder is in a vault (`.obsidian`), MEGA if it's in a MEGA sync (`.megaignore` or `.debris`). It starts that app whenever Claude reads or changes something there, so reads are up to date and changes upload. If it had to start it, the file operation waits until the app has caught up, for at most 45 seconds:
  - **MEGA:** until MEGA reports the folder as synced (the same status as its Explorer icons).
  - **Obsidian:** until the vault is open and its files have stopped changing for a few seconds. Chrome is started whenever a Chrome tool needs it. They're left alone otherwise. `list_dir` with no path, and `bridge.cmd status`, show what was found.
- **Restoring a file:** ask Claude (`restore_file`), or copy it back yourself from `history\<Files|Notes>\...\<file>.<date-time>` (deleted files end in `.deleted`). After 30 days, your sync app's version history or Rubbish bin may still have it.
- **Keeping it reachable:**
  - **Sleep:** set sleep to Never when plugged in, and closing the lid to "Do nothing".
  - **Windows updates:** turn on *Sign-in options > "Use my sign-in info to automatically finish setting up after an update"*, so the bridge comes back after an update restart.
- **Changing folders:** re-run the install command with `-Reconfigure`. It keeps the token:
  ```powershell
  & ([scriptblock]::Create((irm https://github.com/raphlau/ClaudeBridge/releases/latest/download/get.ps1))) -Reconfigure
  ```
- **Checking it end to end:** run `.venv\Scripts\python.exe selftest.py --public`.

## Making a release

Releases are automatic: **whatever is merged into `main` is released**, as long as `VERSION` in `server.py` is new.

1. Make the change on a branch, bump `VERSION` in `server.py`, and run the tests: `uv run python -m unittest discover -s tests`.
2. Open a pull request. The `Release` workflow runs on it: version check, ASCII scripts, Python compiles, tests, dependency audit and the build, without publishing.
3. Merge it. The workflow runs the same checks on `main`, tags `v<VERSION>` and publishes `ClaudeBridge.zip`, its SHA-256, `version.txt` and `get.ps1`. PCs pick it up within a day, or straight away with `bridge.cmd update`.

If a change to anything that ships reaches `main` without a `VERSION` bump, the run fails and GitHub emails you; bump `VERSION` and merge again. Changes to `README.md` or `CLAUDE.md` alone don't make a release; they go out with the next one. Don't push tags by hand.
