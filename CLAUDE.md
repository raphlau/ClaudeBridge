# Claude Bridge: notes for Claude Code

This folder is Claude Bridge, an MCP server (Python, `server.py`) that the user (Raph) adds to Claude as a
custom connector, so Claude can reach his **MEGA folder** and **Obsidian vault** on this Windows PC from his
phone. It's published via **Tailscale Funnel** and protected by a bearer token. See README.md for the design.

## Your job when asked to "set this up"

1. Check prerequisites: `tailscale status` shows this PC connected, MagicDNS is on (the PC has a `*.ts.net`
   name), and the MEGA desktop app (MEGAsync.exe) and Obsidian are installed.
2. Run the installer. It's interactive (`Read-Host` prompts for the two folder paths), so either let Raph run
   `install.cmd` himself, or ask him for the two paths and pre-create `%LOCALAPPDATA%\ClaudeBridge\config.json`.
   To pre-create it, copy `roots` from `config.example.json` (keep `name`, `mode: write` and `sync_app`); the installer
   fills in the token, public URL, MEGA/Obsidian launch details and the Claude app itself.
   Then run `powershell -ExecutionPolicy Bypass -File .\install.ps1`, which keeps an existing config.
3. If Tailscale asks to enable Funnel, Raph has to approve it in the browser; tell him.
4. Verify:
   - `%LOCALAPPDATA%\ClaudeBridge\.venv\Scripts\python.exe %LOCALAPPDATA%\ClaudeBridge\selftest.py --public`
     must print `SELFTEST PASSED`. The public check can fail for up to ~10 minutes while Funnel DNS appears.
   - `bridge.cmd status` and `bridge.cmd logs` help with debugging.
5. Give Raph the connector URL and header from `%LOCALAPPDATA%\ClaudeBridge\connector.txt`. Remind him to add
   the `*.ts.net` name to Claude's network allowlist so download/upload links work.

## Updating an existing install

Unzip the new version and run `install.ps1` from it. It keeps config.json (folders, token, extra settings),
re-detects the apps, restarts the task, and leaves Funnel as is. Then run the self-test.

## Rules

- Keep the security model intact: server binds 127.0.0.1 only, token required on every MCP request, no delete
  or move tools, MEGA/Obsidian can't be quit, `.obsidian`/`.git`/keys hidden, history copy before every change. Don't add a shell or delete tool.
- Never paste the token into anything other than this machine's files or the chat with Raph.
- Windows PowerShell 5.1 compatibility matters (no `&&`, `??`, ternaries); keep .ps1 files ASCII-only.
- After changing `server.py`, restart with `bridge.cmd restart` and re-run the self-test.
- Python deps are locked in `uv.lock`; use `uv sync --frozen` (or `uv lock` if you deliberately change deps).
