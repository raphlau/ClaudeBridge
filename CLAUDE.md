# Claude Bridge: notes for Claude Code

This folder is Claude Bridge, an MCP server (Python, `server.py`) that the user (Raph) adds to Claude as a
custom connector, so Claude can reach two folders on this Windows PC from his phone: **Files** (his documents;
it was called "Backups" before 1.6, and that name still works as an alias) and **Notes** (Markdown notes). It also
passes through Claude in Chrome. It's published via **Tailscale Funnel** and protected by a bearer token. See
README.md for the design.

## Your job when asked to "set this up"

1. Check prerequisites: `tailscale status` shows this PC connected, and MagicDNS is on (the PC has a `*.ts.net`
   name).
2. Raph runs the install one-liner (below) from PowerShell opened from the Start menu. It's interactive: it suggests
   folders (synced document folders, Obsidian vaults from `%APPDATA%\obsidian\obsidian.json`) and he picks.
3. If Tailscale asks to enable Funnel, Raph has to approve it in the browser; tell him.
4. Verify:
   - `%LOCALAPPDATA%\ClaudeBridge\.venv\Scripts\python.exe %LOCALAPPDATA%\ClaudeBridge\selftest.py --public`
     must print `SELFTEST PASSED`. The public check can fail for up to ~10 minutes while Funnel DNS appears.
   - `bridge.cmd status` and `bridge.cmd logs` help with debugging.
5. Give Raph the connector URL and header from `%LOCALAPPDATA%\ClaudeBridge\connector.txt`. Remind him to add
   the `*.ts.net` name to Claude's network allowlist so download/upload links work.

## Installing and updating

- Installs and updates must not run from a Claude desktop app session: it redirects new AppData writes into the app's
  own storage, where the scheduled tasks can't see them. install.ps1 detects this and refuses. Raph runs the
  one-liner from PowerShell opened from the Start menu:
  `irm https://github.com/raphlau/ClaudeBridge/releases/latest/download/get.ps1 | iex`
- Updates run inside the `ClaudeBridge Update` scheduled task, which runs outside the app, so they're safe to start
  from anywhere: `bridge.cmd update`, or the `update_bridge` MCP tool (`install=True`, or `reinstall=True` to
  reinstall the same version). update.ps1 downloads the release, checks its SHA-256, runs
  `install.ps1 -Unattended`, checks /healthz and rolls back from `previous\` if needed. The log is
  `logs\update.log`; the last result is in `update_state.json`.
- Read-only checks (selftest.py, `bridge.cmd status`) and `bridge.cmd restart` are fine from a session. To change
  config.json from a session, edit the existing file in place (rewriting it as a new file gets redirected).

## Releasing

The repo (raphlau/ClaudeBridge) is public; `.github/workflows/release.yml` publishes GitHub releases of it with the
built-in workflow token. Never commit config.json, connector.txt, logs or anything else with the token or personal paths.
1. Bump `VERSION` in server.py and commit.
2. Tag `v<VERSION>` and push the tag.
3. The workflow refuses a tag that doesn't match `VERSION`, and fails on non-ASCII .ps1/.cmd files.
4. Add any new file the install needs to both the workflow's zip list and install.ps1's copy list.

## Rules

- Keep the security model intact: server binds 127.0.0.1 only, token required on every MCP request, no delete
  or move tools, sync apps can't be quit, `.obsidian`/`.git`/keys hidden, history copy before every change,
  no writing programs/scripts/shortcuts (`DENY_WRITE_EXTS`), uploads get Mark-of-the-Web, wrong-token lockout
  (`Guard`). Don't add a shell or delete tool. Never let attacker-controlled text (headers, paths) reach a
  command line: `send_alert` passes the toast XML via an environment variable for that reason.
- Chrome tools are passed through from Claude Code's `claude --claude-in-chrome-mcp` (see `ChromeLink` in `server.py`).
  Keep `file_upload` blocked, including inside `browser_batch`, and don't log the text Claude types. `chrome_status`
  shows the link state; the child's stderr goes to `logs\chrome-mcp.log`. The bridge finds Claude Code by itself
  (including the desktop app's bundled copy, whose versioned folder changes on app updates). The browser choice is
  self-healed in `ChromeLink._settle`: it never auto-picks a browser that isn't known to be on this PC.
- The server must not do anything by itself while unused: no timers or watchers. MEGA/Obsidian are only checked and
  started by write tools (`ensure_for`), history is pruned from `keep_history`, and the Chrome child starts on the
  first Chrome call and stops after `chrome.idle_minutes`. The daily update is a separate scheduled task.
- update.ps1 must stay a no-op while the bridge is switched off (`bridge.cmd off` is the kill switch), and must
  never print the token: install.ps1 -Unattended output goes into logs\update.log.
- Never paste the token into anything other than this machine's files or the chat with Raph.
- Windows PowerShell 5.1 compatibility matters (no `&&`, `??`, ternaries); keep .ps1 files ASCII-only.
- After changing `server.py`, restart with `bridge.cmd restart` and re-run the self-test.
- Python deps are locked in `uv.lock`; use `uv sync --frozen` (or `uv lock` if you deliberately change deps).
