# Claude Bridge

A small background server on your PC that Claude reaches as a **custom connector**,
published to the internet through **Tailscale Funnel**. It gives Claude, from any device (phone included):

- **Read and write access** to two folders: your **MEGA folder** (as `Backups/...`) and your **Obsidian vault** (as `Notes/...`).
- Precise note edits: replace a passage, append to a note, or create a note.
- PDF text, PDF pages and images as pictures, and full-text search across notes.
- Short-lived download and upload links, so Claude can move big or binary files between your PC and its cloud workspace.
- **Sync kept running:** before every change it makes, the bridge checks that the **MEGA app** and **Obsidian** (with your vault open) are running, and starts them if not, so changes get uploaded. It also checks once at sign-in.
- **App control:** Claude can check, start and close **MEGA, Obsidian, Chrome and the Claude desktop app** (`app_status`, `start_app`, `quit_app`). It can't close MEGA or Obsidian, since they do the syncing. This lets you turn on computer access and Claude in Chrome only while you need them.

It doesn't need the Claude desktop app running, and it doesn't use the app's computer-access setting.

## Safety rails

- **Nothing can be deleted or moved.**
- **Existing files keep a copy:** before any change, the previous version is copied to `%LOCALAPPDATA%\ClaudeBridge\history` for 30 days. That folder is outside MEGA and Obsidian, so the copies don't sync. MEGA's and Obsidian's own version history still apply on top.
- **No accidental overwrites:** existing files are only replaced when Claude explicitly asks to overwrite. Edits keep the file's line endings and encoding.
- **Hidden from Claude:** `.obsidian` (plugin settings can hold API keys), `.git`, `.ssh`, MEGA's `.debris`, password databases (`*.kdbx`) and key files. Edit `deny_folders` and `deny_files` in `config.json` to change this.
- **Nothing outside your two folders:** no commands, and symlink or `..` tricks are blocked.
- **No clicks on your screen:** linking a cloud session to the PC is still your decision.
- **Everything is logged:** every tool call goes to `logs\bridge.log`.

## Install (about 10 minutes)

**Before you start:** Tailscale is installed and logged in on this PC, and MagicDNS is on (Tailscale admin console > DNS).

1. Unzip this folder anywhere, for example into Downloads.
2. Double-click `install.cmd`. No admin needed. Or, in PowerShell in that folder, run: `powershell -ExecutionPolicy Bypass -File .\install.ps1`
3. Paste in two paths: your **MEGA folder** (or a subfolder of it) and your **Obsidian vault's top folder**. In Explorer, Shift+right-click a folder and choose "Copy as path".
4. When Tailscale asks you to approve **Funnel** for this PC (a browser page or a link), approve it.

The installer:

- Installs `uv` and a private Python into `%LOCALAPPDATA%\ClaudeBridge`.
- Finds MEGA, Obsidian and the Claude app.
- Writes `config.json` with a random secret token, readable only by you.
- Registers a background task, `ClaudeBridge`, that starts when you sign in and restarts itself if it crashes.
- Runs `tailscale funnel --bg --https=443 http://127.0.0.1:8765`.

The server itself only listens on 127.0.0.1. The outside world reaches it only through Funnel, and only with the token. At the end, the installer prints your connector details and saves them in `connector.txt`.

**Using Claude Code to install:** open Claude Code in this folder and say "set this up". `CLAUDE.md` tells it what to do and how to test it (`selftest.py`).

## Add it to Claude

In Claude: **Settings > Connectors > Add custom connector**.

- **URL:** `https://<your-pc>.<tailnet>.ts.net/mcp`
- **Request header:** `Authorization: Bearer <token>`
- **Fallback:** if the dialog has nowhere to put a header, use the URL with the secret inside it: `https://<your-pc>.<tailnet>.ts.net/k/<token>/mcp`

Then, in the connector's tool permissions, consider setting `write_text`, `edit_text`, `append_text`, `create_upload_link`, `start_app` and `quit_app` to ask before running.

**Download and upload links:** Claude's cloud workspace has a network allowlist. Add your PC's Funnel name (`<your-pc>.<tailnet>.ts.net`) to it, the same place you added MEGA. Without it, everything else still works; only the links won't.

## Claude in Chrome / linking a session to the PC

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

Other commands: `status`, `restart` (after editing `config.json`), `logs`, `test` (run in the foreground to see errors).

## Troubleshooting

- **"Server didn't start":** run `bridge.cmd test` to see the error. Usually it's a folder path in `config.json`; use `\\` in JSON paths.
- **Claude says it can't connect:** run `bridge.cmd status`. New Funnel names can take about 10 minutes to appear in public DNS.
- **Check it end to end:** run `.venv\Scripts\python.exe selftest.py`, or `selftest.py --public` to go through Funnel as well.
- **MEGA, Obsidian, Chrome or Claude not found:** install the app, then re-run `install.cmd`. It keeps your folders, token and any settings you added, and just re-detects the apps.
- **Changing folders or moving PCs:** re-run `install.ps1 -Reconfigure`. It keeps the token, so the connector keeps working.
- **Updating to a new version:** unzip it and run `install.cmd` again. The connector in Claude doesn't need to change, but re-check tool permissions if tool names changed.

## Restoring an earlier version

Find the file in `%LOCALAPPDATA%\ClaudeBridge\history\<Backups|Notes>\...`. Copies are named `<file>.<date-time>`. Copy the one you want back over the current file. Alternatively, use MEGA's or Obsidian's version history.
