# Claude Bridge

A small background server on your PC that Claude reaches as a **custom connector**,
published to the internet through **Tailscale Funnel**. It gives Claude, from any device (phone included):

- **Read and write access** to two folders: your **MEGA folder** (as `Backups/...`) and your **Obsidian vault** (as `Notes/...`).
- Precise note edits: replace a passage, append to a note, or create a note.
- PDF text, PDF pages and images as pictures, and full-text search across notes.
- Short-lived download and upload links, so Claude can move big or binary files between your PC and its cloud workspace.
- **Sync kept running:** before every change it makes, the bridge checks that the **MEGA app** and **Obsidian** (with your vault open) are running, and starts them if not, so changes get uploaded. It also checks once at sign-in.
- **App control:** Claude can check, start and close **MEGA, Obsidian, Chrome and the Claude desktop app** (`app_status`, `start_app`, `quit_app`). It can't close MEGA or Obsidian, since they do the syncing. This lets you turn on computer access only while you need it.
- **Claude in Chrome from any chat:** the bridge passes through Claude in Chrome's browser tools (`navigate`, `computer`, `read_page`, `find`, `get_page_text`, `tabs_context_mcp`...), using Claude Code on this PC. Any chat with the connector can use Chrome here, including one started on your phone that isn't linked to the computer. See [Claude in Chrome](#claude-in-chrome-from-any-chat).
- **Updates itself:** once a day it installs the latest release, and Claude can update it on request (`update_bridge`). See [Updates](#updates).

It doesn't need the Claude desktop app running, and it doesn't use the app's computer-access setting.

## Safety rails

- **Nothing can be deleted or moved.**
- **Existing files keep a copy:** before any change, the previous version is copied to `%LOCALAPPDATA%\ClaudeBridge\history` for 30 days. That folder is outside MEGA and Obsidian, so the copies don't sync. MEGA's and Obsidian's own version history still apply on top.
- **No accidental overwrites:** existing files are only replaced when Claude explicitly asks to overwrite. Edits keep the file's line endings and encoding.
- **Hidden from Claude:** `.obsidian` (plugin settings can hold API keys), `.git`, `.ssh`, MEGA's `.debris`, password databases (`*.kdbx`) and key files. Edit `deny_folders` and `deny_files` in `config.json` to change this.
- **Nothing outside your two folders:** no commands, and symlink or `..` tricks are blocked.
- **No clicks outside Chrome:** the Chrome tools click and type only inside Chrome, through the Claude in Chrome extension and its own site permissions. Nothing else on the screen is touched, and linking a cloud session to the PC is still your decision.
- **No file uploads from the PC to websites:** Claude in Chrome's `file_upload` is blocked (also inside `browser_batch`), since it could send any file on the PC to a web page.
- **Everything is logged:** every tool call goes to `logs\bridge.log`. For Chrome, the log keeps URLs and actions but not the text Claude typed.
- **Updates only from the releases repo:** the updater downloads from `raphlau/ClaudeBridge-releases` only, checks the SHA-256, and won't turn the bridge back on if you switched it off.

## Install (about 10 minutes)

**Before you start:** Tailscale is installed and logged in on this PC, and MagicDNS is on (Tailscale admin console > DNS).

1. Open **PowerShell from the Start menu**. Don't use a terminal inside the Claude app: it redirects AppData, and the installer refuses to run there. No admin needed. Then run:

   ```powershell
   irm https://github.com/raphlau/ClaudeBridge-releases/releases/latest/download/get.ps1 | iex
   ```

   It downloads the latest release, checks its SHA-256 and starts the installer. (Or download `ClaudeBridge.zip` from the [releases page](https://github.com/raphlau/ClaudeBridge-releases/releases), unzip it and double-click `install.cmd`.)
2. Paste in two paths: your **MEGA folder** (or a subfolder of it) and your **Obsidian vault's top folder**. In Explorer, Shift+right-click a folder and choose "Copy as path".
3. When Tailscale asks you to approve **Funnel** for this PC (a browser page or a link), approve it.

The installer:

- Installs `uv` and a private Python into `%LOCALAPPDATA%\ClaudeBridge`.
- Finds MEGA, Obsidian and the Claude app.
- Writes `config.json` with a random secret token, readable only by you.
- Registers a background task, `ClaudeBridge`, that starts when you sign in and restarts itself if it crashes.
- Registers `ClaudeBridge Update`, which installs new releases once a day (see [Updates](#updates)).
- Runs `tailscale funnel --bg --https=443 http://127.0.0.1:8765`.

The server itself only listens on 127.0.0.1. The outside world reaches it only through Funnel, and only with the token. At the end, the installer prints your connector details and saves them in `connector.txt`.

**Using Claude Code to install:** open Claude Code in this folder and say "set this up". `CLAUDE.md` tells it what to do and how to test it (`selftest.py`).

## Add it to Claude

In Claude: **Settings > Connectors > Add custom connector**.

- **URL:** `https://<your-pc>.<tailnet>.ts.net/mcp`
- **Request header:** `Authorization: Bearer <token>`
- **Fallback:** if the dialog has nowhere to put a header, use the URL with the secret inside it: `https://<your-pc>.<tailnet>.ts.net/k/<token>/mcp`

Then, in the connector's tool permissions, consider setting `write_text`, `edit_text`, `append_text`, `create_upload_link`, `start_app` and `quit_app` to ask before running. The Chrome tools that only read pages are marked read-only; the ones that click, type, navigate or run scripts (`computer`, `form_input`, `navigate`, `javascript_tool`, `browser_batch`...) are not, so you can set those to ask too.

**Download and upload links:** Claude's cloud workspace has a network allowlist. Add your PC's Funnel name (`<your-pc>.<tailnet>.ts.net`) to it, the same place you added MEGA. Without it, everything else still works; only the links won't.

## Claude in Chrome from any chat

The bridge runs the Claude in Chrome server that ships with **Claude Code** (`claude --claude-in-chrome-mcp`) and passes its browser tools through to the connector. So a chat started on your phone can browse in Chrome on this PC without being linked to the computer, and without the Claude desktop app.

**One-time setup on this PC:**

1. **Claude Code:** the bridge finds it by itself. It looks for a standalone install (`%USERPROFILE%\.local\bin\claude.exe`, PATH, npm), and otherwise uses the copy bundled with the Claude desktop app. That copy's folder changes with every app update, so the bridge looks it up each time and moves to the new one when Chrome is idle. To pin one yourself, set `"chrome": {"claude_path": "C:\\path\\to\\claude.exe"}` in `config.json`; if that path disappears, the bridge goes back to searching.
2. **Sign in once:** in PowerShell from the Start menu, run that `claude.exe` (for the app's copy: `%LOCALAPPDATA%\Claude-Data\claude_*\claude-code\<version>\<build>\claude.exe`), then `/login` with the **same claude.ai account** as the Chrome extension. The desktop app signs in its own sessions itself, but the bridge runs Claude Code on its own, so it needs this sign-in. It's kept in `%USERPROFILE%\.claude` and survives app updates.
3. `bridge.cmd restart`, then the self-test. `chrome_status` should say *connected* and name the browser.

**Using it:** just ask, for example "Open my Amazon orders in Chrome on my laptop". Claude starts Chrome with `start_app` if it isn't running.

**Which browser:** if several browsers are signed in to your account, the bridge picks one without asking anyone:
- the browser already in use;
- otherwise the one used last here, saved in `chrome_state.json`;
- otherwise `"chrome": {"device_id": ...}` in `config.json`;
- otherwise the one Claude Code knows is on this PC.

It never picks a browser on another computer by itself. If none of these fits (for example after reinstalling the extension), Claude asks you in the chat and calls `select_browser`, and the bridge remembers that choice. You never need to touch the PC for it. `chrome_status` shows which browser is in use and why.

**Site permissions:** by default the Claude in Chrome extension's own site settings apply. If a site isn't allowed there, the extension asks in Chrome on the PC, which you can't answer from your phone. Either allow the sites you use in the extension's settings, or set `"chrome": {"permission_mode": "skip_all_permission_checks"}` in `config.json` so it never asks (this lets Claude act on any site). `ask` and `follow_a_plan` are the other values Claude Code accepts.

**Other settings** (all under `"chrome"` in `config.json`): `"enabled": false` turns the Chrome tools off; `"device_id"` is the starting choice of browser (a deviceId from `list_connected_browsers`); `"call_timeout_seconds"` (default 180).

**If it doesn't work:** ask Claude to run `chrome_status` (it can also restart the connection with `restart=true`), or check `logs\chrome-mcp.log`. Claude Code and the extension must be signed in to the same account, and Chrome must be running with the extension.

## Updates

Releases are published to the public repo [raphlau/ClaudeBridge-releases](https://github.com/raphlau/ClaudeBridge-releases/releases). The source repo stays private.

- **Daily:** the `ClaudeBridge Update` task runs `update.ps1` around 04:00 to 06:00, or later if the PC was off.
- **What an update does:** if there's a newer release, it downloads `ClaudeBridge.zip`, checks its SHA-256, and runs the new installer unattended. Your folders, token, connector and settings are kept.
- **If it fails:** if the new version doesn't start, the update puts the previous files back (kept in `previous\`).
- **Kill switch:** it does nothing while the bridge is switched off (`bridge.cmd off`).
- **On request:** ask Claude to "update the bridge" (`update_bridge` checks; `install=True` installs; `reinstall=True` reinstalls the current release). Or run `bridge.cmd update` on the PC.
- **Status:** `bridge.cmd status` and `update_bridge` show the version and the last update result. Details are in `logs\update.log`.
- **Turn off the daily update:** set `"update": {"auto": false}` in `config.json`. `update_bridge(install=True)` still works.

**Making a release** (in the source repo):
1. Bump `VERSION` in `server.py` and commit.
2. Push a matching tag: `git tag v1.4.1`, then `git push origin main v1.4.1`.
3. The `Release` workflow checks that the tag matches `VERSION`, checks that the `.ps1`/`.cmd` files are ASCII and that the Python compiles, then builds the zip and publishes it to both repos.

**One-time setup** for the public releases:
- **Releases repo:** a public `raphlau/ClaudeBridge-releases` repo with at least one commit (for example a README).
- **Token:** an Actions secret named `RELEASES_TOKEN` in the source repo, holding a fine-grained token with **Contents: read and write** on the releases repo only.

## Linking a session to the PC

The Claude desktop app only gives remote access while it's **open**. So leave it closed, and ask Claude to start it when you need it:

1. "Start Chrome and the Claude app on my PC." Claude calls `start_app` for each and waits until it's up. Windows must be signed in; a locked screen is fine. If Claude in Chrome is in a non-default Chrome profile, re-run the installer with `-ChromeProfile "Profile 1"` (use your profile's folder name, as shown at `chrome://version` under Profile Path).
2. Use it:
   - If your account has **Dispatch**, message it from your phone. Nothing on the PC needs clicking.
   - Otherwise, link a cloud session as usual. If that needs a click in the desktop app, that click stays with you.
3. When you're done: "Quit the Claude app (and Chrome) on my PC." Chrome is closed as if you'd clicked X, so your tabs come back next time.

## Keeping it reachable (laptop or desktop)

- **Sleep (laptop):** set sleep to Never when plugged in, and the lid to "Do nothing" when plugged in. Both are under Control Panel > Power Options > "Choose what closing the lid does". When the laptop sleeps, the bridge is offline.
- **Windows updates:** turn on *Settings > Accounts > Sign-in options > "Use my sign-in info to automatically finish setting up after an update"*, so the PC signs back in (locked) after an update restart and the bridge comes back by itself.
- **Wake-on-LAN (desktop, optional):**
  1. Enable it in the BIOS. On some boards this also means turning off ErP or deep sleep.
  2. In Device Manager > network adapter > Power Management, allow it to wake the PC with a magic packet.
  3. Turn off Fast Startup.
  4. Send the packet from your router's app or another always-on device. Tailscale can't wake a sleeping PC by itself.
- **Keeping MEGA and Obsidian up:** by default the bridge only checks them at sign-in and before its own changes. To have it re-check every N minutes, set `"sync_watchdog_minutes": N` in `config.json`. Note that it would then reopen them if you close them yourself.

## Turning it off

- **At the PC:** run `bridge.cmd off` in `%LOCALAPPDATA%\ClaudeBridge`. It stops the server and takes it off the internet. `bridge.cmd on` reverses it.
- **From your phone:** in the Tailscale admin console, disable this machine, or remove the `funnel` attribute from the tailnet policy.
- **New secret:** `bridge.cmd rotate-token` makes a new token. Update the connector in Claude afterwards.
- **Remove it:** `bridge.cmd uninstall`, then delete the folder.

Other commands: `status`, `restart` (after editing `config.json`), `logs`, `update` (install the latest release now), `test` (run in the foreground to see errors).

## Troubleshooting

- **"Server didn't start":** run `bridge.cmd test` to see the error. Usually it's a folder path in `config.json`; use `\\` in JSON paths.
- **Claude says it can't connect:** run `bridge.cmd status`. New Funnel names can take about 10 minutes to appear in public DNS.
- **Check it end to end:** run `.venv\Scripts\python.exe selftest.py`, or `selftest.py --public` to go through Funnel as well.
- **MEGA, Obsidian, Chrome or Claude not found:** install the app, then re-run `install.cmd`. It keeps your folders, token and any settings you added, and just re-detects the apps.
- **Changing folders or moving PCs:** re-run `install.ps1 -Reconfigure`. It keeps the token, so the connector keeps working.
- **Updating to a new version:** it happens by itself (see [Updates](#updates)), or run `bridge.cmd update`. Re-running the install command also works. The connector in Claude doesn't need to change, but re-check tool permissions if tool names changed.
- **An update failed:** see `logs\update.log`. The previous version is put back automatically, and its files stay in `previous\`.

## Restoring an earlier version

Find the file in `%LOCALAPPDATA%\ClaudeBridge\history\<Backups|Notes>\...`. Copies are named `<file>.<date-time>`. Copy the one you want back over the current file. Alternatively, use MEGA's or Obsidian's version history.
