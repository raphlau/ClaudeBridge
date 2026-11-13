"""Claude Bridge: a small MCP server that gives Claude scoped access to chosen
folders on this PC, start/quit control of a few desktop apps, and Claude in
Chrome on this PC (through Claude Code's own Chrome integration).

Exposed to Claude as a custom connector via Tailscale Funnel. Every request
must carry the secret token (Authorization: Bearer <token>, or the /k/<token>/
URL prefix). Folders are read-only unless their mode is "write"; nothing is
ever deleted.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, Tool, ToolAnnotations
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

VERSION = "1.6.0"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CLAUDE_BRIDGE_CONFIG", BASE_DIR / "config.json"))
IS_WINDOWS = sys.platform == "win32"

TEXT_EXTS = {
    ".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".log", ".ini", ".cfg", ".conf", ".toml", ".py", ".js", ".ts", ".ps1", ".sh", ".bat",
    ".rtf", ".tex", ".org", ".canvas", ".base",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

# ---------------------------------------------------------------- config / logging


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # tolerate a BOM from PowerShell
        cfg = json.load(f)
    token = cfg.get("token", "")
    # install.ps1 makes 43 random characters (256 bits); refuse anything short or obviously hand-made
    if len(token) < 32 or len(set(token)) < 16 or not re.fullmatch(r"[A-Za-z0-9_\-]+", token):
        raise SystemExit("config.json: 'token' must be a long random string (run 'bridge.cmd rotate-token' to make one)")
    if not cfg.get("roots"):
        raise SystemExit("config.json: add at least one entry to 'roots'")
    return cfg


CFG = load_config()
LIMITS = {
    "max_read_lines": 2000,
    "max_text_bytes": 5_000_000,
    "max_pdf_chars": 80_000,
    "max_search_files": 20_000,
    "max_upload_mb": 500,
    "link_minutes": 15,
    **CFG.get("limits", {}),
}
# Any path containing one of these folder names is hidden (case-insensitive).
DENY_FOLDERS = {s.lower() for s in CFG.get("deny_folders", [".git", ".obsidian", ".ssh", ".gnupg", ".debris", "$recycle.bin"])}
# Any file whose name matches one of these globs is hidden.
DENY_FILES = [s.lower() for s in CFG.get("deny_files", ["*.kdbx", "*.kdb", ".env", "*.pem", "*.key", "id_rsa*", "id_ed25519*"])]
# Never written, created or uploaded: things Windows runs or follows when opened or merely shown in Explorer
# (programs, scripts, shortcuts, installers). Reading them is still allowed.
DENY_WRITE_EXTS = {s.lower() for s in CFG.get("deny_write_exts", [
    ".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp", ".msc", ".dll", ".sys", ".ocx", ".bat", ".cmd",
    ".ps1", ".psm1", ".psd1", ".ps1xml", ".psc1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".wsc", ".hta",
    ".sct", ".jar", ".py", ".pyw", ".sh", ".reg", ".inf", ".lnk", ".url", ".website", ".scf", ".library-ms",
    ".searchconnector-ms", ".appref-ms", ".application", ".appx", ".msix", ".msixbundle", ".appinstaller",
    ".settingcontent-ms", ".chm", ".xll", ".iqy", ".slk", ".gadget"])}
DENY_WRITE_NAMES = {"desktop.ini", "autorun.inf"}
WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)

LOG_DIR = Path(CFG.get("log_dir") or BASE_DIR / "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
_handler = RotatingFileHandler(LOG_DIR / "bridge.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log = logging.getLogger("bridge")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False
if sys.stderr is not None and (not CFG.get("quiet") or os.environ.get("CLAUDE_BRIDGE_VERBOSE")):
    log.addHandler(logging.StreamHandler())
# pythonw has no console: give stray prints somewhere harmless to go
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# ---------------------------------------------------------------- roots and paths


@dataclass
class Root:
    name: str
    path: Path
    writable: bool
    sync_app: str = ""  # name of an app (in CFG["apps"]) that syncs this folder, started when the bridge changes something
    description: str = ""  # what the folder is for, shown to Claude


ROOTS: dict[str, Root] = {}
for r in CFG["roots"]:
    name = r["name"].strip().strip("/\\")
    if not name or "/" in name or "\\" in name:
        raise SystemExit(f"config.json: bad root name {r['name']!r}")
    ROOTS[name.lower()] = Root(name, Path(r["path"]).expanduser(), r.get("mode", "read") == "write", r.get("sync_app", ""),
                               r.get("description", ""))
# Old names still work in paths (e.g. 'Backups/...' after the folder was renamed to 'Files')
ALIASES: dict[str, Root] = {}
for r in CFG["roots"]:
    aliases = r.get("aliases") or []
    for a in [aliases] if isinstance(aliases, str) else aliases:
        if a.lower() not in ROOTS:
            ALIASES[a.lower()] = ROOTS[r["name"].strip().strip("/\\").lower()]


class BridgeError(ToolError):
    """An anticipated failure: the message is shown to Claude."""


def _denied(rel: str, is_dir: bool = False) -> bool:
    parts = [p.lower() for p in rel.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    folders = parts if is_dir else parts[:-1]
    if any(p in DENY_FOLDERS for p in folders):
        return True
    return not is_dir and any(fnmatch.fnmatch(parts[-1], pat) for pat in DENY_FILES)


def resolve(vpath: str, *, want_write: bool = False, must_exist: bool = True) -> tuple[Root, Path, str]:
    """Map 'RootName/sub/path' to a real path inside that root, or raise."""
    vpath = (vpath or "").replace("\\", "/").strip().strip("/")
    if not vpath:
        raise BridgeError("Give a path starting with a folder name: " + ", ".join(r.name for r in ROOTS.values()))
    if "\x00" in vpath or re.match(r"^[A-Za-z]:", vpath):
        raise BridgeError("Use paths like 'Notes/sub/file.md', not drive paths")
    head, _, rest = vpath.partition("/")
    root = ROOTS.get(head.lower()) or ALIASES.get(head.lower())
    if root is None:
        raise BridgeError(f"Unknown folder {head!r}. Available: " + ", ".join(r.name for r in ROOTS.values()))
    parts = [p for p in rest.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise BridgeError("'..' is not allowed")
    for p in parts:
        # ':' would reach NTFS alternate data streams; Windows silently drops trailing dots/spaces, and device
        # names (CON, NUL...) aren't files. All three can make a name mean something other than it looks.
        if ":" in p or p != p.rstrip(". ") or WIN_RESERVED.match(p) or any(ord(c) < 32 for c in p):
            raise BridgeError(f"Not a usable file or folder name: {p!r}")
    if want_write and parts:
        name = parts[-1].lower()
        if name in DENY_WRITE_NAMES or any(name.endswith(ext) for ext in DENY_WRITE_EXTS):
            raise BridgeError("The bridge doesn't write programs, scripts or shortcuts (they could run on the PC). "
                              "Save it under another extension, e.g. .txt, and Raph can rename it.")
    base = root.path.resolve()
    target = base.joinpath(*parts) if parts else base
    real = target.resolve()
    if real != base and base not in real.parents:
        raise BridgeError("That path points outside the shared folder")
    rel = "/".join(parts)
    real_rel = real.relative_to(base).as_posix() if real != base else ""
    is_dir = real.is_dir()
    if (rel and _denied(rel, is_dir)) or (real_rel and _denied(real_rel, is_dir)):
        raise BridgeError("That path is excluded by the bridge's deny list")
    if want_write and not root.writable:
        raise BridgeError(f"'{root.name}' is read-only. Writable folders: " + (", ".join(r.name for r in ROOTS.values() if r.writable) or "none"))
    if must_exist and not real.exists():
        raise BridgeError(f"Not found: {root.name}/{rel}")
    return root, real, f"{root.name}/{rel}".rstrip("/")


def vname(root: Root, p: Path) -> str:
    rel = p.resolve().relative_to(root.path.resolve()).as_posix()
    return f"{root.name}/{rel}" if rel != "." else root.name


def walk(root: Root, start: Path):
    """Yield files under start, skipping denied paths and symlinks that escape."""
    base = root.path.resolve()
    for dirpath, dirnames, filenames in os.walk(start):
        d = Path(dirpath)
        keep = []
        for dn in dirnames:
            full = d / dn
            try:
                rel = full.resolve().relative_to(base).as_posix()
            except ValueError:
                continue
            if not _denied(rel, is_dir=True):
                keep.append(dn)
        dirnames[:] = sorted(keep)
        for fn in sorted(filenames):
            full = d / fn
            try:
                rel = full.resolve().relative_to(base).as_posix()
            except ValueError:
                continue
            if not _denied(rel):
                yield full, rel


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def audit(tool: str, **kw: Any) -> None:
    short = {k: (v if not isinstance(v, str) or len(v) < 200 else v[:200] + "...") for k, v in kw.items()}
    log.info("tool %s %s", tool, json.dumps(short, ensure_ascii=False, default=str))


# ---------------------------------------------------------------- one-time links

LINKS: dict[str, dict[str, Any]] = {}


def _new_link(kind: str, real: Path, display: str, minutes: int, overwrite: bool = False, root: "Root | None" = None) -> str:
    now = time.time()
    for t in [t for t, v in LINKS.items() if v["expires"] < now]:
        LINKS.pop(t, None)
    minutes = max(1, min(int(minutes or LIMITS["link_minutes"]), 120))
    tok = secrets.token_urlsafe(32)
    LINKS[tok] = {"kind": kind, "path": real, "display": display, "expires": now + minutes * 60, "overwrite": overwrite, "root": root}
    base = CFG.get("public_url", "").rstrip("/")
    if not base:
        raise BridgeError("Set 'public_url' in config.json to use links")
    return f"{base}/{'dl' if kind == 'download' else 'ul'}/{tok}"



# ---------------------------------------------------------------- safe writes

HISTORY_DIR = Path(CFG.get("history_dir") or BASE_DIR / "history")
HISTORY_DAYS = int(CFG.get("history_days", 30))
HISTORY_MAX_BYTES = 100 * 1024 * 1024


_last_prune = 0.0


def keep_history(root: Root, real: Path) -> str:
    """Copy a file into the bridge's local history (outside the synced folders) before it changes.
    Old copies are cleared out here too (at most once a day), so nothing runs while the bridge isn't used."""
    global _last_prune
    if time.time() - _last_prune > 86400:
        _last_prune = time.time()
        try:
            prune_history()
        except Exception:  # noqa: BLE001 - never block a write over housekeeping
            log.exception("history clean-up failed")
    if not real.is_file():
        return ""
    if real.stat().st_size > HISTORY_MAX_BYTES:
        return " (too big for local history; the sync app's own version history still applies)"
    rel = real.resolve().relative_to(root.path.resolve())
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    dest = HISTORY_DIR / root.name / rel.parent / f"{rel.name}.{stamp}"
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{rel.name}.{stamp}-{n}")
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(real, dest)
    return " (previous version kept in the bridge's history)"


def prune_history() -> None:
    if not HISTORY_DIR.exists():
        return
    cutoff = time.time() - HISTORY_DAYS * 86400
    for f in HISTORY_DIR.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def mark_from_internet(real: Path) -> None:
    """Give a file the bridge created the same 'downloaded from the internet' mark a browser would (an NTFS
    Zone.Identifier stream), so Windows SmartScreen and Office Protected View treat it with caution."""
    if not IS_WINDOWS:
        return
    try:
        with open(f"{real}:Zone.Identifier", "w", encoding="ascii", newline="") as f:
            f.write("[ZoneTransfer]\r\nZoneId=3\r\nReferrerUrl=claude-bridge\r\n")
    except OSError:
        pass  # not NTFS (e.g. a FAT drive): nothing to mark


def read_for_edit(real: Path) -> tuple[str, bool, bool]:
    """Return (text with \\n line endings, file used CRLF, file had a BOM)."""
    if real.stat().st_size > LIMITS["max_text_bytes"]:
        raise BridgeError("File too large to edit as text")
    raw = real.read_bytes()
    if b"\x00" in raw[:8192]:
        raise BridgeError("That's a binary file; only text files can be edited")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig" if bom else "utf-8") if raw else ""
    except UnicodeDecodeError:
        raise BridgeError("That file isn't UTF-8 text, so it can't be edited safely here; use create_upload_link to replace it")
    crlf = "\r\n" in text
    return text.replace("\r\n", "\n"), crlf, bom


def write_atomic(real: Path, text: str, crlf: bool = False, bom: bool = False) -> None:
    """Write bytes exactly (no newline translation), via a temp file so sync apps never see half a file."""
    if crlf:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
    real.parent.mkdir(parents=True, exist_ok=True)
    tmp = real.with_name(f".{real.name}.bridge-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, real)


# ---------------------------------------------------------------- desktop apps (MEGA, Obsidian, Chrome, Claude)

CLAUDE_MATCH = ["\\windowsapps\\claude_", "\\anthropicclaude\\", "\\programs\\claude\\"]


@dataclass
class App:
    name: str
    process: str                 # exe name, e.g. "MEGAsync.exe"
    launch: Any = None           # command list, or a URI string such as obsidian://open?vault=...
    match: list[str] | None = None  # the exe path must contain one of these (tells the desktop app from Claude Code's CLI)
    vault: str = ""              # Obsidian: this vault must be open for Sync to upload
    can_quit: bool = False
    purpose: str = ""


def _as_list(v: Any) -> list[str] | None:
    if not v:
        return None
    return [v] if isinstance(v, str) else list(v)


def _load_apps() -> dict[str, App]:
    apps: dict[str, App] = {}
    for a in CFG.get("apps", []):
        name = a["name"]
        default_match = CLAUDE_MATCH if a["process"].lower() == "claude.exe" else None
        apps[name.lower()] = App(name, a["process"], a.get("launch"), _as_list(a.get("match")) or default_match,
                                 a.get("vault", ""), bool(a.get("can_quit", False)), a.get("purpose", ""))
    # configs written by v1.1 used separate keys
    for a in CFG.get("sync_apps", []):
        apps.setdefault(a["name"].lower(), App(a["name"], a["process"], a.get("launch"), None, a.get("vault", "")))
    old = CFG.get("claude_app") or {}
    if old.get("launch") and "claude" not in apps:
        apps["claude"] = App("Claude", "claude.exe", old["launch"], _as_list(old.get("exe_match")) or CLAUDE_MATCH, can_quit=True)
    return apps


APPS = _load_apps()


def _procs(app: App) -> list:
    import psutil

    want = app.process.lower()
    match = [m.lower() for m in (app.match or [])]
    found = []
    for p in psutil.process_iter(["name", "exe", "create_time"]):
        if (p.info.get("name") or "").lower() != want:
            continue
        exe = (p.info.get("exe") or "").lower()
        if match and exe and not any(m in exe for m in match):
            continue
        found.append(p)
    return found


def _obsidian_vault_open(vault_path: str) -> bool | None:
    """Obsidian records which vaults have a window open in %APPDATA%/obsidian/obsidian.json."""
    cfg = Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    want = os.path.normcase(os.path.abspath(vault_path))
    for v in (data.get("vaults") or {}).values():
        if os.path.normcase(os.path.abspath(v.get("path", ""))) == want:
            return bool(v.get("open"))
    return None


def _launch(cmd: Any) -> str:
    """Start something on the signed-in desktop (the bridge runs inside that session)."""
    try:
        if isinstance(cmd, str) and "://" in cmd and IS_WINDOWS:
            os.startfile(cmd)  # e.g. obsidian://open?vault=...
        else:
            subprocess.Popen(cmd, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return ""
    except OSError as e:
        return str(e)


def app_state(app: App) -> tuple[bool, str]:
    """(ready, description). Obsidian is only 'ready' when the vault is open, since Sync needs it."""
    running = bool(_procs(app))
    if not running:
        return False, "not running"
    if app.vault:
        vopen = _obsidian_vault_open(app.vault)
        if vopen is False:
            return False, "running, but the vault isn't open"
        return True, "running, vault open" if vopen else "running"
    return True, "running"


def get_app(name: str) -> App:
    app = APPS.get((name or "").strip().lower())
    if not app:
        raise BridgeError(f"Unknown app {name!r}. Configured: " + (", ".join(a.name for a in APPS.values()) or "none"))
    return app


def ensure_app(name: str, start: bool = True) -> str:
    """Make sure an app is running (and for Obsidian, the vault is open); start it if not."""
    app = APPS.get((name or "").lower())
    if not app:
        return ""
    if not IS_WINDOWS:
        return f"{app.name}: can't check on this OS"
    ready, state = app_state(app)
    if ready:
        return f"{app.name}: {state}"
    if not start or not app.launch:
        return f"{app.name}: {state.upper() if state == 'not running' else state}"
    err = _launch(app.launch)
    if err:
        log.warning("couldn't start %s: %s", app.name, err)
        return f"{app.name}: {state}, and starting it failed ({err})"
    log.info("started %s (%s)", app.name, state)
    return f"{app.name}: was {state}, started it"


def ensure_for(root: Root) -> str:
    msg = ensure_app(root.sync_app)
    return f"\n{msg}" if msg else ""


def _quit(app: App) -> int:
    """Close gracefully (like clicking X) so Chrome keeps its tabs, then force anything left."""
    import psutil

    procs = _procs(app)
    if not procs:
        return 0
    pids = [str(p.pid) for p in procs]
    args = ["taskkill"] + [x for pid in pids for x in ("/PID", pid)]
    subprocess.run(args, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _, alive = psutil.wait_procs(procs, timeout=10)
    for p in alive:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return len(procs)


def _logged_in_users() -> list[str]:
    try:
        import psutil

        return sorted({u.name for u in psutil.users()})
    except Exception:
        return []


# ---------------------------------------------------------------- Claude in Chrome (through Claude Code)
#
# Claude Code ships the Claude in Chrome MCP server that it normally starts for itself
# (`claude --claude-in-chrome-mcp`, stdio). The bridge runs it as a child process, signed in with
# Claude Code's own login, and passes its browser tools through. So any chat with this connector can
# use Chrome on this PC, including one started on a phone that isn't linked to the computer.
# The child lives in its own thread and event loop, so it isn't tied to any one HTTP request. It only runs while
# Chrome is in use: it starts on the first Chrome call and stops after chrome.idle_minutes without one.

CHROME = {
    "enabled": True,
    "claude_path": "",          # Claude Code's CLI; found automatically when empty (or when the path no longer exists)
    "permission_mode": "",      # "" = the extension's own site permissions; or ask / skip_all_permission_checks / follow_a_plan
    "device_id": "",            # starting choice of browser (deviceId); a browser picked later is remembered in chrome_state.json
    "call_timeout_seconds": 180,
    "idle_minutes": 10,         # stop Claude Code's Chrome server after this long without a Chrome call
    **(CFG.get("chrome") or {}),
}
CHROME_PERMISSION_MODES = ("ask", "skip_all_permission_checks", "follow_a_plan")
# file_upload would hand any file on this PC to a web page, outside the shared folders and deny lists.
CHROME_BLOCKED = {"file_upload"}
CHROME_READ_ONLY = {"read_page", "find", "get_page_text", "read_console_messages", "read_network_requests",
                    "list_connected_browsers"}
CHROME_CACHE = BASE_DIR / "chrome_tools.json"  # last tool list, so tools/list is instant while the child starts
CHROME_STATE = BASE_DIR / "chrome_state.json"  # the browser last used, so a choice made in chat sticks
CHROME_PICKERS = {"select_browser", "switch_browser"}


def _desktop_app_clis() -> list[Path]:
    """Claude Code CLIs bundled with the Claude desktop app, newest first. The app keeps one in
    %LOCALAPPDATA%\\Claude-Data\\claude_<id>\\claude-code\\<version>\\<build>\\claude.exe and replaces the
    version folder when it updates, so it's looked up each time rather than saved."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Claude-Data"

    def key(p: Path) -> tuple:
        try:
            ver = tuple(int(x) for x in p.parent.parent.name.split("."))
        except ValueError:
            ver = ()
        return ver, p.stat().st_mtime

    try:
        found = [p for p in base.glob("claude_*/claude-code/*/*/claude.exe") if p.is_file()]
        return sorted(found, key=key, reverse=True)
    except OSError:
        return []


def find_claude_code() -> str:
    """Path to Claude Code's CLI (not the desktop app, whose exe is also claude.exe), or ''.
    Order: chrome.claude_path if it still exists, a standalone install, then the copy bundled with the desktop app."""
    want = str(CHROME.get("claude_path") or "").strip()
    if want and Path(want).expanduser().is_file():
        return want
    cands = [Path.home() / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude")]
    found = shutil.which("claude")
    if found:
        cands.append(Path(found))
    if IS_WINDOWS:
        cands.append(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd")
    for c in cands:
        low = str(c).lower()
        if any(m in low for m in CLAUDE_MATCH) or "\\microsoft\\windowsapps\\" in low:
            continue  # the desktop app or its Store alias
        if c.is_file():
            return str(c)
    if IS_WINDOWS:
        for c in _desktop_app_clis():
            return str(c)
    return ""


def _err(e: BaseException) -> str:
    """One readable line, unwrapping anyio's exception groups."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _text(res: Any) -> str:
    return "\n".join(c.text for c in (getattr(res, "content", None) or []) if getattr(c, "type", "") == "text")


def _chrome_audit_args(args: dict[str, Any]) -> dict[str, Any]:
    """Log what Claude did in Chrome, but not what it typed (form values can be personal)."""
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in ("text", "value") and isinstance(v, str):
            out[k] = f"<{len(v)} chars>"
        elif k == "actions" and isinstance(v, list):
            out[k] = [{"name": a.get("name"), **_chrome_audit_args(a.get("input") or {})} if isinstance(a, dict) else "?" for a in v]
        else:
            out[k] = v
    return out


class ChromeLink:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self.state = "off" if not CHROME.get("enabled", True) else "idle"
        self.error = ""
        self.cli = ""
        self.since = 0.0
        self.started_at = 0.0
        self._session: ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._kick: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None
        self._loop_ready = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.browser = ""              # which browser calls go to, for chrome_status
        self._browser_ok = False
        self._browser_checked = 0.0
        self._last_used = 0.0
        self._inflight = 0
        self._saved: dict[str, Any] = {}
        if CHROME.get("enabled", True):
            self._load_cache()
            try:
                self._saved = json.loads(CHROME_STATE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    # -- lifecycle (runs in the bridge-chrome thread)

    def start(self) -> None:
        if not CHROME.get("enabled", True) or self._thread:
            return
        self.started_at = time.time()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), name="bridge-chrome", daemon=True)
        self._thread.start()

    def _wanted(self) -> bool:
        idle = max(1.0, float(CHROME.get("idle_minutes", 10))) * 60
        return time.time() - self._last_used < idle

    def use(self) -> None:
        """Note that Chrome is being used, and start Claude Code's Chrome server if it isn't running."""
        self._last_used = time.time()
        self.start()
        self._loop_ready.wait(5)
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def wait_ready(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self._ready.is_set() and time.time() < end and self.state != "Claude Code not found":
            await asyncio.to_thread(self._ready.wait, 0.5)

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._kick = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop_ready.set()
        delay = 5
        while True:
            if not self._wanted():
                # Nothing running and nothing to do until the next Chrome call.
                self.state = "idle"
                self._wake.clear()
                await self._wake.wait()
                delay = 5
            began = time.time()
            try:
                await self._connect_once()
                delay = 1  # a requested restart, or stopped for being idle
            except Exception as e:  # noqa: BLE001 - the child failed to start, or died
                self.error = _err(e)
                log.warning("chrome: link down: %s", self.error)
            self._session = None
            self._ready.clear()
            if self.state == "connected":
                self.state = "reconnecting"
            if not self._wanted():
                continue  # nobody is using Chrome: go idle rather than retry
            if time.time() - began > 300:
                delay = 5
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def _connect_once(self) -> None:
        if self._kick:
            self._kick.clear()  # a restart asked for while the last connection was already going down
        self.cli = find_claude_code()
        if not self.cli:
            self.state = "Claude Code not found"
            raise RuntimeError("Claude Code (the claude CLI) isn't installed for this Windows user")
        env = dict(os.environ)
        mode = str(CHROME.get("permission_mode") or "").strip()
        if mode in CHROME_PERMISSION_MODES:
            env["CLAUDE_CHROME_PERMISSION_MODE"] = mode
        device = self._saved.get("device_id") or CHROME.get("device_id")
        if device:
            env["CLAUDE_CHROME_PAIRED_DEVICE_ID"] = str(device)
        params = StdioServerParameters(command=self.cli, args=["--claude-in-chrome-mcp"], env=env, cwd=str(BASE_DIR))
        self.state = "starting"
        err_path = LOG_DIR / "chrome-mcp.log"
        fresh = err_path.exists() and err_path.stat().st_size > 5_000_000
        with open(err_path, "w" if fresh else "a", encoding="utf-8", errors="replace") as errlog:
            errlog.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting {self.cli} --claude-in-chrome-mcp\n")
            errlog.flush()
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), 60)
                    listed = (await asyncio.wait_for(session.list_tools(), 30)).tools
                    self.tools = {t.name: t for t in listed if t.name not in CHROME_BLOCKED}
                    self._save_cache()
                    self._session, self.error, self.state, self.since = session, "", "connected", time.time()
                    log.info("chrome: Claude Code's Chrome server is up (%s), %d tools", self.cli, len(self.tools))
                    await self._settle_quietly(session)  # before the first call goes through
                    self._ready.set()
                    while True:  # stay up while in use; ping so a dead child is noticed
                        try:
                            await asyncio.wait_for(self._kick.wait(), 10)
                            self._kick.clear()
                            log.info("chrome: restart requested")
                            return
                        except TimeoutError:
                            pass
                        if self._inflight == 0 and not self._wanted():
                            # Leaving also means the next start picks up a newer Claude Code if the app updated.
                            log.info("chrome: not used for %s min, stopping Claude Code's Chrome server",
                                     CHROME.get("idle_minutes", 10))
                            return
                        await asyncio.wait_for(session.send_ping(), 20)

    # -- choosing the browser (runs in the bridge-chrome thread)

    async def _browsers(self, session: ClientSession) -> list[dict[str, Any]]:
        res = await asyncio.wait_for(session.call_tool("list_connected_browsers", {}), 30)
        text = _text(res).lstrip()
        if getattr(res, "is_error", False) or not text.startswith("["):
            return []  # e.g. "Browser extension is not connected"
        data, _ = json.JSONDecoder().raw_decode(text)
        return [b for b in data if isinstance(b, dict) and b.get("deviceId")]

    async def _settle(self, session: ClientSession) -> None:
        """Make sure calls go to the right browser without anyone picking one by hand. Uses the browser in use,
        else the one last used here, the one in config.json, or the one Claude Code knows is on this PC. A browser
        on another computer is never picked automatically: then Claude asks, and the answer is remembered."""
        self._browser_checked = time.time()
        browsers = await self._browsers(session)
        if not browsers:
            self._browser_ok, self.browser = False, "none connected (is Chrome running with the extension?)"
            return
        pick, why = next((b for b in browsers if b.get("inUse")), None), "in use"
        if not pick:
            by_id = {b["deviceId"]: b for b in browsers}
            local = [b["deviceId"] for b in browsers if b.get("onThisComputer")]
            for cand, reason in ((self._saved.get("device_id"), "used last"), (CHROME.get("device_id"), "set in config.json"),
                                 (local[0] if len(local) == 1 else "", "on this PC")):
                if cand and cand in by_id:
                    pick, why = by_id[cand], reason
                    break
            if not pick:
                self._browser_ok = False
                self.browser = (f"not chosen: {len(browsers)} connected, none known to be this PC's. "
                                "Ask Raph which one, then call select_browser; the bridge remembers it.")
                return
            res = await asyncio.wait_for(session.call_tool("select_browser", {"deviceId": pick["deviceId"]}), 30)
            if getattr(res, "is_error", False):
                self._browser_ok, self.browser = False, f"couldn't select {pick.get('name')}: {_text(res)[:200]}"
                log.warning("chrome: %s", self.browser)
                return
            log.info("chrome: selected browser %s (%s, %s)", pick.get("name"), pick["deviceId"], why)
        self._browser_ok = True
        self.browser = f"{pick.get('name') or '?'} ({pick['deviceId'][:8]}..., {why})"
        if pick["deviceId"] != self._saved.get("device_id"):
            self._saved = {"device_id": pick["deviceId"], "name": pick.get("name", ""), "saved": fmt_time(time.time())}
            try:
                CHROME_STATE.write_text(json.dumps(self._saved), encoding="utf-8")
            except OSError as e:
                log.warning("chrome: couldn't save the browser choice: %s", e)

    async def _settle_quietly(self, session: ClientSession) -> None:
        try:
            await self._settle(session)
        except Exception as e:  # noqa: BLE001 - never let this take the link down
            self._browser_ok, self.browser = False, f"couldn't check ({_err(e)})"
            log.warning("chrome: browser check failed: %s", _err(e))

    def restart(self) -> None:
        if self._loop and self._kick:
            self._loop.call_soon_threadsafe(self._kick.set)

    def _load_cache(self) -> None:
        try:
            for t in json.loads(CHROME_CACHE.read_text(encoding="utf-8")):
                tool = Tool.model_validate(t)
                if tool.name not in CHROME_BLOCKED:
                    self.tools[tool.name] = tool
        except (OSError, ValueError):
            pass

    def _save_cache(self) -> None:
        try:
            data = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in self.tools.values()]
            CHROME_CACHE.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError) as e:
            log.warning("chrome: couldn't cache the tool list: %s", e)

    # -- used by the MCP server (runs in uvicorn's event loop)

    async def tool_list(self) -> list[Tool]:
        if not CHROME.get("enabled", True):
            return []
        if not self.tools:
            # Very first use on this PC: nothing cached yet, so start Claude Code's Chrome server to learn its tools.
            await asyncio.to_thread(self.use)
            await self.wait_ready(15)
        out = []
        for t in self.tools.values():
            ann = ToolAnnotations(title=t.title or None, read_only_hint=t.name in CHROME_READ_ONLY,
                                  destructive_hint=False, open_world_hint=True)
            out.append(t.model_copy(update={"annotations": ann}))
        return out

    def handles(self, name: str) -> bool:
        return CHROME.get("enabled", True) and (name in self.tools or name in CHROME_BLOCKED)

    async def call(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name in CHROME_BLOCKED or (name == "browser_batch" and any(
                isinstance(a, dict) and a.get("name") in CHROME_BLOCKED for a in (arguments.get("actions") or []))):
            raise BridgeError("file_upload isn't available through the bridge: it could send any file on the PC to a "
                              "web page. Ask Raph to pick the file in the browser himself.")
        audit(f"chrome.{name}", **_chrome_audit_args(arguments))
        timeout = float(CHROME.get("call_timeout_seconds", 180))
        await asyncio.to_thread(self.use)  # starts Claude Code's Chrome server if it's idle
        for attempt in (1, 2):
            if not self._ready.is_set():
                await self.wait_ready(45 if attempt == 1 else 20)
            session, loop = self._session, self._loop
            if session is None or loop is None:
                raise BridgeError(f"Claude in Chrome isn't reachable through the bridge right now ({self.error or self.state}). "
                                  "Call chrome_status for details.")
            # Re-check the browser choice now and then (Chrome restarted, extension reinstalled...).
            age = time.time() - self._browser_checked
            if name not in CHROME_PICKERS and ((not self._browser_ok and age > 30) or age > 900):
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._settle_quietly(session), loop))
            sent = time.time()
            fut = asyncio.run_coroutine_threadsafe(session.call_tool(name, arguments, read_timeout_seconds=timeout), loop)
            self._inflight += 1
            try:
                result = await asyncio.wrap_future(fut)
                if name in CHROME_PICKERS and not getattr(result, "is_error", False):
                    # Remember the browser Raph picked, so the next chat doesn't have to ask.
                    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._settle_quietly(session), loop))
                return result
            except Exception as e:  # noqa: BLE001 - a transport failure or timeout, not a tool error
                log.warning("chrome: %s failed: %s", name, _err(e))
                # The child died between pings: restart it. Retry only if the call failed at once,
                # i.e. it never reached the browser, so nothing can happen twice.
                if "closed" in _err(e).lower() and self._session is session:
                    self._ready.clear()
                    self.restart()
                    if attempt == 1 and time.time() - sent < 1:
                        await asyncio.sleep(1)
                        continue
                raise BridgeError(f"The Chrome call didn't complete ({_err(e)}). Call chrome_status, then retry.") from None
            finally:
                self._inflight -= 1
                self._last_used = time.time()  # the idle time counts from the end of the last call
        raise BridgeError("The Chrome call didn't complete. Call chrome_status, then retry.")

    def describe(self) -> str:
        if not CHROME.get("enabled", True):
            return "Chrome tools: turned off in config.json (\"chrome\": {\"enabled\": false})"
        idle_min = CHROME.get("idle_minutes", 10)
        if self.state == "connected":
            head = f"Chrome tools: connected since {fmt_time(self.since)} ({len(self.tools)} tools; stops after {idle_min} min unused)"
        elif self.state == "idle":
            head = (f"Chrome tools: idle. Claude Code's Chrome server starts on the next Chrome call and stops after "
                    f"{idle_min} min unused" + (f" (last error: {self.error})" if self.error else ""))
        else:
            head = f"Chrome tools: NOT connected ({self.state}{': ' + self.error if self.error else ''})"
        mode = str(CHROME.get("permission_mode") or "").strip()
        lines = [
            head,
            f"Claude Code: {self.cli or find_claude_code() or 'not found'}",
            "Site permissions: " + (mode if mode in CHROME_PERMISSION_MODES else "the extension's own settings"),
        ]
        if self.state == "connected":
            lines.insert(1, f"Browser: {self.browser or 'not checked yet'}")
        elif self._saved.get("device_id"):
            lines.insert(1, f"Browser: {self._saved.get('name') or '?'} ({str(self._saved['device_id'])[:8]}..., used last)")
        want = str(CHROME.get("claude_path") or "").strip()
        if want and not Path(want).expanduser().is_file():
            lines.append(f"Note: chrome.claude_path in config.json no longer exists ({want}); found Claude Code by itself instead.")
        chrome_app = APPS.get("chrome")
        if chrome_app and IS_WINDOWS:
            lines.append(f"Chrome: {app_state(chrome_app)[1]}")
        if self.state not in ("connected", "idle") or self.error:
            lines.append("To fix: Claude Code must be installed on the PC and signed in (run `claude`, then /login) with the same "
                         "claude.ai account as the Chrome extension. Details are in logs\\chrome-mcp.log on the PC.")
        return "\n".join(lines)


CHROME_LINK = ChromeLink()


# ---------------------------------------------------------------- MCP server

DEFAULT_DESCRIPTIONS = {
    "files": "his documents and other files (PDFs, scans, letters, office files, images)",
    "notes": "his notes (Markdown files; an Obsidian vault)",
}


def _folder_lines() -> str:
    lines = []
    for r in ROOTS.values():
        what = r.description or DEFAULT_DESCRIPTIONS.get(r.name.lower(), "a shared folder")
        lines.append(f"- {r.name}: {what}{'' if r.writable else ' (read-only)'}")
    return "\n".join(lines)


INSTRUCTIONS = f"""Claude Bridge v{VERSION}: access to folders on Raph's PC.
{_folder_lines()}
Paths always start with the folder name, e.g. 'Notes/Insurance.md'. Changes sync to the cloud and his other devices,
so change only what was asked. Prefer edit_text/append_text over rewriting whole notes. Nothing can be deleted or moved,
and the bridge keeps the previous version of anything it changes. It won't write programs, scripts or shortcuts.
For big or binary files (docx, xlsx, zip...), use create_download_link and fetch the URL from your own workspace.
app_status / start_app / quit_app manage the apps the bridge knows (Chrome, the Claude desktop app, sync apps).
Chrome on this PC: the browser tools here (tabs_context_mcp, navigate, computer, read_page, find, get_page_text...) are
Claude in Chrome, run through Claude Code on the PC, so they work from any chat, linked or not, without the Claude app.
Start with tabs_context_mcp(createIfEmpty=true) and work in your own tab. If a call says Chrome or the extension isn't
connected, call start_app('Chrome'), wait a few seconds and retry; chrome_status shows what's wrong. The bridge picks
this PC's browser by itself; if list_connected_browsers says none is selected, ask Raph which one, then call
select_browser (the bridge remembers it). file_upload is not available.
update_bridge checks for a newer version of the bridge and can install it; it also updates itself once a day."""


class BridgeServer(MCPServer):
    """MCPServer plus the Chrome tools passed through from Claude Code's Claude in Chrome server."""

    async def list_tools(self) -> list[Tool]:
        own = await super().list_tools()
        names = {t.name for t in own}
        return own + [t for t in await CHROME_LINK.tool_list() if t.name not in names]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if self._tool_manager.get_tool(name) is None and CHROME_LINK.handles(name):
            return await CHROME_LINK.call(name, arguments or {})
        return await super().call_tool(name, arguments, context)


mcp = BridgeServer(name="Claude Bridge", instructions=INSTRUCTIONS, version=VERSION)
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WR = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


@mcp.tool(annotations=RO)
def list_dir(path: str = "", recursive: bool = False, max_entries: int = 300) -> str:
    """List a folder. With no path, lists the shared folders. recursive=True lists files in subfolders too."""
    audit("list_dir", path=path, recursive=recursive)
    if not path.strip().strip("/"):
        lines = ["Shared folders:"]
        for r in ROOTS.values():
            state = "ok" if r.path.exists() else "MISSING on disk"
            lines.append(f"- {r.name}  ({'writable' if r.writable else 'read-only'}, {state})")
        return "\n".join(lines)
    root, real, disp = resolve(path)
    if real.is_file():
        st = real.stat()
        return f"{disp}  {fmt_size(st.st_size)}  {fmt_time(st.st_mtime)}"
    max_entries = max(1, min(max_entries, 5000))
    out, n = [], 0
    if recursive:
        for full, _ in walk(root, real):
            st = full.stat()
            out.append(f"{vname(root, full)}  {fmt_size(st.st_size)}  {fmt_time(st.st_mtime)}")
            n += 1
            if n >= max_entries:
                out.append(f"... stopped at {max_entries} entries")
                break
    else:
        for child in sorted(real.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                rel = child.resolve().relative_to(root.path.resolve()).as_posix()
            except ValueError:
                continue
            if _denied(rel, is_dir=child.is_dir()):
                continue
            if child.is_dir():
                out.append(f"[dir]  {child.name}/")
            else:
                st = child.stat()
                out.append(f"       {child.name}  {fmt_size(st.st_size)}  {fmt_time(st.st_mtime)}")
            n += 1
            if n >= max_entries:
                out.append(f"... stopped at {max_entries} entries")
                break
    return f"{disp}:\n" + ("\n".join(out) if out else "(empty)")


@mcp.tool(annotations=RO)
def find_files(pattern: str, path: str = "", max_results: int = 200) -> str:
    """Find files by name. pattern is a case-insensitive substring, or a glob if it contains * or ?.
    path limits the search to one folder (default: all shared folders)."""
    audit("find_files", pattern=pattern, path=path)
    pat = pattern.lower()
    is_glob = any(c in pat for c in "*?[")
    targets = [resolve(path)[:2]] if path.strip("/ ") else [(r, r.path) for r in ROOTS.values() if r.path.exists()]
    out, scanned = [], 0
    for root, start in targets:
        for full, rel in walk(root, start):
            scanned += 1
            name = full.name.lower()
            if (fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel.lower(), pat)) if is_glob else (pat in rel.lower()):
                st = full.stat()
                out.append(f"{root.name}/{rel}  {fmt_size(st.st_size)}  {fmt_time(st.st_mtime)}")
                if len(out) >= max_results:
                    return "\n".join(out) + f"\n... stopped at {max_results} results"
            if scanned >= LIMITS["max_search_files"]:
                return "\n".join(out) + f"\n... stopped after scanning {scanned} files"
    return "\n".join(out) if out else "No matching files"


@mcp.tool(annotations=RO)
def search_text(query: str, path: str = "", regex: bool = False, file_glob: str = "*",
                max_results: int = 100, context_lines: int = 0) -> str:
    """Search inside text files (md, txt, csv, json...) for a phrase (case-insensitive) or regex.
    Use path to limit to one folder and file_glob (e.g. '*.md') to filter files."""
    audit("search_text", query=query, path=path, regex=regex, file_glob=file_glob)
    try:
        rx = re.compile(query if regex else re.escape(query), re.IGNORECASE)
    except re.error as e:
        raise BridgeError(f"Bad regex: {e}")
    targets = [resolve(path)[:2]] if path.strip("/ ") else [(r, r.path) for r in ROOTS.values() if r.path.exists()]
    out, scanned, hits = [], 0, 0
    ctx = max(0, min(context_lines, 5))
    for root, start in targets:
        files = [(start, start.name)] if start.is_file() else walk(root, start)
        for full, rel in files:
            if full.suffix.lower() not in TEXT_EXTS or not fnmatch.fnmatch(full.name.lower(), file_glob.lower()):
                continue
            scanned += 1
            if scanned > LIMITS["max_search_files"]:
                out.append(f"... stopped after scanning {scanned - 1} files")
                return "\n".join(out)
            try:
                if full.stat().st_size > LIMITS["max_text_bytes"]:
                    continue
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if rx.search(line):
                    hits += 1
                    disp = vname(root, full)
                    if ctx:
                        lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
                        block = "\n".join(f"  {j + 1}: {lines[j][:300]}" for j in range(lo, hi))
                        out.append(f"{disp}:{i + 1}\n{block}")
                    else:
                        out.append(f"{disp}:{i + 1}: {line.strip()[:300]}")
                    if hits >= max_results:
                        out.append(f"... stopped at {max_results} matches")
                        return "\n".join(out)
    return "\n".join(out) if out else f"No matches in {scanned} text files"


@mcp.tool(annotations=RO)
def read_text(path: str, start_line: int = 1, max_lines: int = 500) -> str:
    """Read a text file (notes, csv, json...). Use start_line/max_lines to page through long files."""
    audit("read_text", path=path, start_line=start_line)
    root, real, disp = resolve(path)
    if not real.is_file():
        raise BridgeError(f"{disp} is a folder; use list_dir")
    if real.stat().st_size > LIMITS["max_text_bytes"]:
        raise BridgeError("File too large to read as text; use create_download_link")
    raw = real.read_bytes()
    if b"\x00" in raw[:8192]:
        raise BridgeError("This looks like a binary file; use read_pdf, view_file or create_download_link")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    start = max(1, start_line)
    count = max(1, min(max_lines, LIMITS["max_read_lines"]))
    chunk = lines[start - 1:start - 1 + count]
    end = start + len(chunk) - 1
    more = f"\n[lines {start}-{end} of {len(lines)}; continue with start_line={end + 1}]" if end < len(lines) else ""
    return f"{disp} (lines {start}-{end} of {len(lines)})\n" + "\n".join(chunk) + more


@mcp.tool(annotations=RO)
def read_pdf(path: str, start_page: int = 1, end_page: int = 0) -> str:
    """Extract text from a PDF. end_page=0 means up to the last page (subject to a size cap).
    Scanned PDFs have no text layer: use view_file to look at the page instead."""
    audit("read_pdf", path=path, start_page=start_page, end_page=end_page)
    import pymupdf

    root, real, disp = resolve(path)
    with pymupdf.open(real) as doc:
        n = doc.page_count
        a = max(1, start_page)
        b = n if end_page <= 0 else min(end_page, n)
        parts, total = [], 0
        for i in range(a - 1, b):
            text = doc[i].get_text().strip()
            block = f"--- page {i + 1} ---\n{text or '(no text layer on this page)'}"
            if total + len(block) > LIMITS["max_pdf_chars"] and parts:
                parts.append(f"[stopped at page {i}; continue with start_page={i + 1}]")
                break
            parts.append(block)
            total += len(block)
    return f"{disp} ({n} pages)\n" + "\n".join(parts)


@mcp.tool(annotations=RO)
def view_file(path: str, page: int = 1, dpi: int = 110) -> Image:
    """Look at an image, or at one page of a PDF rendered as an image (for scans, forms, receipts)."""
    audit("view_file", path=path, page=page)
    root, real, disp = resolve(path)
    ext = real.suffix.lower()
    if ext == ".pdf":
        import pymupdf

        with pymupdf.open(real) as doc:
            if not 1 <= page <= doc.page_count:
                raise BridgeError(f"Page must be 1-{doc.page_count}")
            pix = doc[page - 1].get_pixmap(dpi=max(50, min(dpi, 200)))
            return Image(data=pix.tobytes("png"), format="png")
    if ext in IMAGE_EXTS:
        from PIL import Image as PILImage

        with PILImage.open(real) as im:
            im = im.convert("RGB")
            im.thumbnail((1568, 1568))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            return Image(data=buf.getvalue(), format="jpeg")
    raise BridgeError("view_file handles PDFs and images; use read_text or create_download_link for other files")


@mcp.tool(annotations=RO)
def create_download_link(path: str, minutes: int = 15) -> str:
    """Make a short-lived HTTPS link to download one file (any type, any size), e.g. to process it in your own workspace."""
    audit("create_download_link", path=path, minutes=minutes)
    root, real, disp = resolve(path)
    if not real.is_file():
        raise BridgeError("Links are for files, not folders")
    url = _new_link("download", real, disp, minutes)
    return f"{url}\nFile: {disp} ({fmt_size(real.stat().st_size)}). Valid for {min(max(1, minutes), 120)} minutes."


@mcp.tool(annotations=WR)
def write_text(path: str, content: str, overwrite: bool = False) -> str:
    """Create a text file (e.g. a new note). Won't replace an existing file unless overwrite=True;
    to change part of an existing file prefer edit_text or append_text."""
    audit("write_text", path=path, overwrite=overwrite, chars=len(content))
    root, real, disp = resolve(path, want_write=True, must_exist=False)
    if real.exists() and (real.is_dir() or not overwrite):
        raise BridgeError(f"{disp} already exists (use edit_text/append_text, or overwrite=True to replace it)")
    crlf = bom = False
    note = ""
    if real.exists():
        _, crlf, bom = read_for_edit(real)
        note = keep_history(root, real)
    write_atomic(real, content.replace("\r\n", "\n"), crlf, bom)
    return f"Saved {disp} ({fmt_size(real.stat().st_size)}){note}" + ensure_for(root)


@mcp.tool(annotations=WR)
def edit_text(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """Replace an exact piece of text in a text file (e.g. a note). old_text must appear exactly once
    unless replace_all=True. Read the file first so old_text matches exactly. Line endings and encoding are kept."""
    audit("edit_text", path=path, old_chars=len(old_text), new_chars=len(new_text), replace_all=replace_all)
    root, real, disp = resolve(path, want_write=True)
    if not real.is_file():
        raise BridgeError(f"{disp} is not a file")
    text, crlf, bom = read_for_edit(real)
    old = old_text.replace("\r\n", "\n")
    if not old:
        raise BridgeError("old_text is empty; use append_text to add to a file")
    count = text.count(old)
    if count == 0:
        raise BridgeError("old_text wasn't found. Read the file again and copy the text exactly (including spaces and line breaks)")
    if count > 1 and not replace_all:
        raise BridgeError(f"old_text appears {count} times; add more surrounding text to make it unique, or set replace_all=True")
    note = keep_history(root, real)
    write_atomic(real, text.replace(old, new_text.replace("\r\n", "\n")), crlf, bom)
    return f"Edited {disp} ({count} replacement{'s' if count > 1 else ''}){note}" + ensure_for(root)


@mcp.tool(annotations=WR)
def append_text(path: str, text: str, create: bool = True) -> str:
    """Add text to the end of a text file (starting on a new line). Creates the file if missing unless create=False."""
    audit("append_text", path=path, chars=len(text))
    root, real, disp = resolve(path, want_write=True, must_exist=False)
    if real.is_dir():
        raise BridgeError(f"{disp} is a folder")
    if real.exists():
        current, crlf, bom = read_for_edit(real)
        note = keep_history(root, real)
    elif create:
        current, crlf, bom, note = "", False, False, ""
    else:
        raise BridgeError(f"Not found: {disp}")
    sep = "\n" if current and not current.endswith("\n") else ""
    write_atomic(real, current + sep + text.replace("\r\n", "\n"), crlf, bom)
    return f"Appended to {disp}{note}" + ensure_for(root)


@mcp.tool(annotations=WR)
def create_upload_link(path: str, overwrite: bool = False, minutes: int = 15) -> str:
    """Make a one-time HTTPS link to upload a file (any type) into a writable folder.
    Upload with: curl -T <localfile> <url>"""
    audit("create_upload_link", path=path, overwrite=overwrite)
    root, real, disp = resolve(path, want_write=True, must_exist=False)
    if real.exists() and (real.is_dir() or not overwrite):
        raise BridgeError(f"{disp} already exists (pass overwrite=True to replace a file)")
    url = _new_link("upload", real, disp, minutes, overwrite, root)
    return (f"{url}\nPUT the file body to this URL (curl -T file URL). One use, valid {min(max(1, minutes), 120)} minutes, "
            f"max {LIMITS['max_upload_mb']} MB." + ensure_for(root))


@mcp.tool(annotations=WR)
def make_folder(path: str) -> str:
    """Create a folder (and parents) inside a writable folder."""
    audit("make_folder", path=path)
    root, real, disp = resolve(path, want_write=True, must_exist=False)
    real.mkdir(parents=True, exist_ok=True)
    return f"Folder ready: {disp}" + ensure_for(root)


@mcp.tool(annotations=RO)
def app_status() -> str:
    """Which desktop apps the bridge knows are running on the PC: the apps that sync the shared folders, Chrome,
    and the Claude desktop app."""
    audit("app_status")
    if not APPS:
        return "No apps configured"
    lines = []
    for app in APPS.values():
        state = app_state(app)[1] if IS_WINDOWS else "can't check on this OS"
        extra = " (can quit)" if app.can_quit else ""
        lines.append(f"{app.name}: {state}{extra}")
    synced = [f"{r.name} -> {r.sync_app}" for r in ROOTS.values() if r.sync_app]
    if synced:
        lines.append("Synced folders: " + ", ".join(synced))
    users = _logged_in_users()
    lines.append(f"Signed-in Windows users: {', '.join(users) if users else 'none detected'}")
    return "\n".join(lines)


@mcp.tool(annotations=WR)
async def start_app(name: str) -> str:
    """Start a desktop app on the PC if it isn't running: one named by app_status, e.g. 'Chrome' or 'Claude'
    (a notes app also gets its vault opened). Needs someone signed in to Windows
    (a locked screen is fine)."""
    audit("start_app", name=name)
    app = get_app(name)
    if not IS_WINDOWS:
        return "App control only works on Windows"
    ready, state = app_state(app)
    if ready:
        return f"{app.name} is already {state}."
    if not app.launch:
        raise BridgeError(f"No launch command for {app.name} in config.json (re-run install.cmd after installing it)")
    err = _launch(app.launch)
    if err:
        raise BridgeError(f"Couldn't start {app.name}: {err}")
    for _ in range(30):
        await asyncio.sleep(1)
        ready, state = app_state(app)
        if ready:
            hint = " Give it ~20 seconds to connect before linking a session." if app.name.lower() == "claude" else ""
            return f"{app.name} is {state}.{hint}"
    users = _logged_in_users()
    return (f"Asked {app.name} to start, but it isn't ready after 30 seconds ({state}). "
            + ("Nobody seems to be signed in to Windows; apps can only start in a signed-in session."
               if not users else "Check app_status again shortly."))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
async def quit_app(name: str) -> str:
    """Close a desktop app on the PC: 'Claude' (ends any remote link to the computer) or 'Chrome'
    (closed like clicking X, so tabs come back next time). Apps that sync the shared folders can't be closed from here."""
    audit("quit_app", name=name)
    app = get_app(name)
    if not app.can_quit:
        raise BridgeError(f"{app.name} keeps your files syncing, so the bridge won't close it")
    if not IS_WINDOWS:
        return "App control only works on Windows"
    if not _procs(app):
        return f"{app.name} isn't running."
    n = await asyncio.to_thread(_quit, app)
    await asyncio.sleep(1)
    return f"{app.name} closed." if not _procs(app) else f"Some {app.name} processes are still running; check app_status."


@mcp.tool(annotations=RO)
async def chrome_status(restart: bool = False) -> str:
    """Is Claude in Chrome on the PC reachable through the bridge? Shows the link state, Claude Code's path, the
    browser in use and the last error. 'idle' is normal: the link starts by itself on the next Chrome call.
    restart=True (re)starts the bridge's connection to Claude Code's Chrome server and waits for it (try it if
    Chrome calls keep failing while Chrome is running)."""
    audit("chrome_status", restart=restart)
    if restart and CHROME_LINK.state not in ("off",):
        await asyncio.to_thread(CHROME_LINK.use)
        CHROME_LINK.restart()
        await asyncio.sleep(2)
        await CHROME_LINK.wait_ready(45)
    return CHROME_LINK.describe()


# ---------------------------------------------------------------- updates
#
# Releases are GitHub releases of raphlau/ClaudeBridge (public). The 'ClaudeBridge Update' scheduled task
# runs update.ps1 once a day; update_bridge(install=True) asks it to run now. update.ps1 downloads the release,
# checks its SHA-256, runs its installer unattended and puts the old files back if the new version doesn't start.

UPDATE = {"auto": True, "repo": "raphlau/ClaudeBridge", **(CFG.get("update") or {})}
UPDATE_TASK = "ClaudeBridge Update"
UPDATE_STATE = BASE_DIR / "update_state.json"      # written by update.ps1
UPDATE_REQUEST = BASE_DIR / "update_request.json"  # read (and deleted) by update.ps1


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except ValueError:
        return ()


def latest_release() -> str:
    url = f"https://github.com/{UPDATE['repo']}/releases/latest/download/version.txt"
    req = urllib.request.Request(url, headers={"User-Agent": f"ClaudeBridge/{VERSION}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(64).decode("utf-8", "replace").strip()


@mcp.tool(annotations=WR)
async def update_bridge(install: bool = False, reinstall: bool = False) -> str:
    """Check whether a newer version of Claude Bridge is out. install=True upgrades the bridge on the PC to the latest
    release (about a minute; this connection drops while it restarts, then call update_bridge() again to see the
    result). reinstall=True reinstalls the latest release even if it's already running. The PC also checks and
    installs updates by itself once a day."""
    audit("update_bridge", install=install, reinstall=reinstall)
    err = ""
    try:
        latest = await asyncio.to_thread(latest_release)
        if not _version_tuple(latest):
            err, latest = f"unexpected answer {latest[:20]!r}", ""
    except Exception as e:  # noqa: BLE001 - offline, GitHub down, no release yet
        latest, err = "", _err(e)
    try:
        last = json.loads(UPDATE_STATE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        last = {}
    newer = bool(latest) and _version_tuple(latest) > _version_tuple(VERSION)
    lines = [
        f"Running: {VERSION}",
        f"Latest release: {latest + (' (newer)' if newer else '') if latest else 'unknown (' + err + ')'}",
        "Daily automatic update: " + ("on" if UPDATE.get("auto", True) else "off (\"update\": {\"auto\": false} in config.json)"),
    ]
    if last:
        lines.append(f"Last update run: {last.get('at', '?')}: {last.get('result', '?')}"
                     + (f" ({last['detail']})" if last.get("detail") else ""))
    if not (install or reinstall):
        if newer:
            lines.append("Call update_bridge(install=True) to install it now.")
        return "\n".join(lines)
    if not IS_WINDOWS:
        raise BridgeError("Updating only works on Windows")
    if not latest:
        raise BridgeError(f"Couldn't check the releases page ({err}), so nothing was installed.")
    if not newer and not reinstall:
        return "\n".join(lines + ["Already up to date; nothing to install."])
    UPDATE_REQUEST.write_text(json.dumps({"reinstall": reinstall, "at": fmt_time(time.time())}), encoding="utf-8")
    r = await asyncio.to_thread(subprocess.run, ["schtasks", "/Run", "/TN", UPDATE_TASK], capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        UPDATE_REQUEST.unlink(missing_ok=True)
        raise BridgeError(f"Couldn't start the '{UPDATE_TASK}' task ({(r.stderr or r.stdout).strip()}). "
                          "Raph needs to run the installer on the PC once to set it up.")
    return (f"{'Reinstalling' if not newer else 'Updating to'} {latest}. The bridge restarts in about a minute, so this "
            "connection will drop; then call update_bridge() to see the result.")


# ---------------------------------------------------------------- HTTP routes (links, health)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request):
    return PlainTextResponse("ok")


@mcp.custom_route("/dl/{token}", methods=["GET"])
async def download(request: Request):
    info = LINKS.get(request.path_params["token"])
    if not info or info["kind"] != "download" or info["expires"] < time.time() or not info["path"].is_file():
        GUARD.bad_attempt("download link", _client(request.scope))
        return PlainTextResponse("Link not found or expired", status_code=404)
    log.info("download %s", info["display"])
    return FileResponse(info["path"], filename=info["path"].name,
                        media_type=mimetypes.guess_type(info["path"].name)[0] or "application/octet-stream")


@mcp.custom_route("/ul/{token}", methods=["PUT", "POST"])
async def upload(request: Request):
    tok = request.path_params["token"]
    info = LINKS.get(tok)
    if not info or info["kind"] != "upload" or info["expires"] < time.time():
        GUARD.bad_attempt("upload link", _client(request.scope))
        return PlainTextResponse("Link not found or expired", status_code=404)
    LINKS.pop(tok, None)  # one use
    dest: Path = info["path"]
    if dest.exists() and not info["overwrite"]:
        return PlainTextResponse("File already exists", status_code=409)
    root = info["root"]
    if dest.exists():
        keep_history(root, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.bridge-upload")
    cap = LIMITS["max_upload_mb"] * 1024 * 1024
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > cap:
                    raise BridgeError("too large")
                f.write(chunk)
        os.replace(tmp, dest)
        mark_from_internet(dest)
    except BridgeError:
        tmp.unlink(missing_ok=True)
        return PlainTextResponse("Upload too large", status_code=413)
    except Exception:
        tmp.unlink(missing_ok=True)
        log.exception("upload failed")
        return PlainTextResponse("Upload failed", status_code=500)
    log.info("upload %s (%d bytes)", info["display"], size)
    sync = ensure_app(root.sync_app)
    return JSONResponse({"saved": info["display"], "bytes": size, "sync": sync})


# ---------------------------------------------------------------- lockout and alerts
#
# Internet scanners hit the public address all day without any token; those are only logged. A request that
# presents a WRONG token (Bearer header, /k/ URL, or a one-time link) means someone is trying to get in: after
# security.max_bad_tokens of those within security.window_minutes, the bridge locks itself (every request is
# refused, even with the right token) for security.lockout_minutes, and alerts Raph.

SECURITY = {"max_bad_tokens": 10, "window_minutes": 10, "lockout_minutes": 60, "alert_url": "",
            **(CFG.get("security") or {})}
LOCK_FILE = BASE_DIR / "lockout.json"  # survives restarts; 'bridge.cmd unlock' removes it


def _client(scope: dict) -> str:
    """Who sent it (Funnel passes the real address in X-Forwarded-For). Only a valid IP is ever used."""
    import ipaddress

    raw = dict(scope.get("headers") or []).get(b"x-forwarded-for", b"").decode("latin-1").split(",")[0].strip()
    raw = raw or (scope.get("client") or ("", 0))[0]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return "unknown"


def send_alert(title: str, body: str) -> None:
    """A Windows notification on the PC, plus a push to security.alert_url if set (e.g. an ntfy.sh topic)."""
    log.warning("ALERT: %s: %s", title, body)
    if IS_WINDOWS:
        from xml.sax.saxutils import escape

        xml = (f"<toast scenario='reminder'><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
               f"<text>{escape(body)}</text></binding></visual><actions><action content='OK' arguments='ok'/></actions></toast>")
        ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
              "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
              "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:CB_TOAST);"
              "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
              "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
              ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                             env={**os.environ, "CB_TOAST": xml}, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            log.warning("couldn't show the alert on the PC: %s", e)
    url = str(SECURITY.get("alert_url") or "").strip()
    if url.startswith("https://"):
        try:
            req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                         headers={"Title": title, "Priority": "high", "Tags": "warning",
                                                  "User-Agent": f"ClaudeBridge/{VERSION}"})
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't send the alert to alert_url: %s", _err(e))


class Guard:
    def __init__(self) -> None:
        self.bad: list[float] = []
        self.until = 0.0
        self._lock = threading.Lock()
        try:
            self.until = float(json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("until", 0))
        except (OSError, ValueError, AttributeError):
            pass

    def locked(self) -> bool:
        return time.time() < self.until

    def bad_attempt(self, kind: str, who: str) -> None:
        now = time.time()
        window = float(SECURITY.get("window_minutes", 10)) * 60
        limit = max(3, int(SECURITY.get("max_bad_tokens", 10)))
        with self._lock:
            self.bad = [t for t in self.bad if now - t < window] + [now]
            log.warning("wrong token (%s) from %s: %d in the last %d min", kind, who, len(self.bad), window // 60)
            if len(self.bad) < limit or self.locked():
                return
            minutes = max(1, int(SECURITY.get("lockout_minutes", 60)))
            self.until = now + minutes * 60
            count, self.bad = len(self.bad), []
            try:
                LOCK_FILE.write_text(json.dumps({"until": self.until, "until_text": fmt_time(self.until), "since": fmt_time(now),
                                                 "wrong_tokens": count, "last_from": who}), encoding="utf-8")
            except OSError as e:
                log.warning("couldn't save the lockout: %s", e)
        threading.Thread(target=send_alert, daemon=True, args=(
            "Claude Bridge locked",
            f"{count} requests with a wrong token in {int(window // 60)} min (last from {who}). The bridge refuses "
            f"everything until {fmt_time(self.until)[-5:]}. If that wasn't you, run 'bridge.cmd rotate-token' on the PC.")).start()


GUARD = Guard()


# ---------------------------------------------------------------- auth wrapper


async def _respond(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class TokenGate:
    """Require the secret token on everything except /healthz and the one-time link routes (which carry their
    own random token). Accepts 'Authorization: Bearer <token>' or a '/k/<token>' URL prefix. While locked
    (see Guard), everything but /healthz is refused."""

    LINK_PREFIXES = ("/dl/", "/ul/")

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path: str = scope.get("path", "")
        if path == "/healthz":
            return await self.app(scope, receive, send)
        if GUARD.locked():
            await asyncio.sleep(1.0)
            return await _respond(send, 503, json.dumps({"error": "Claude Bridge is locked after repeated wrong-token "
                                  f"requests, until {fmt_time(GUARD.until)}. Unlock on the PC: bridge.cmd unlock"}).encode())
        if path.startswith(self.LINK_PREFIXES):
            return await self.app(scope, receive, send)
        ok = presented = False
        if path.startswith("/k/"):
            presented = True
            _, _, rest = path[3:].partition("/")
            key = path[3:].split("/", 1)[0].encode()
            if hmac.compare_digest(key, self.token):
                ok = True
                scope = dict(scope, path="/" + rest, raw_path=("/" + rest).encode())
        else:
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            presented = bool(auth.strip())
            if auth[:7].lower() == b"bearer " and hmac.compare_digest(auth[7:].strip(), self.token):
                ok = True
        if not ok:
            who = _client(scope)
            if presented:
                GUARD.bad_attempt("connector", who)
            else:  # no token at all: internet background noise
                log.info("rejected request to %s from %s", path[:40] if not path.startswith("/k/") else "/k/***", who)
            await asyncio.sleep(1.0)
            return await _respond(send, 401, b'{"error":"unauthorized"}')
        return await self.app(scope, receive, send)


def build_app():
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # Requests arrive via Tailscale Funnel with the public host name; the token protects us.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return TokenGate(app, CFG["token"])


def main():
    for name in ("uvicorn.error", "mcp"):
        lg = logging.getLogger(name)
        lg.addHandler(_handler)
        lg.setLevel(logging.WARNING)
    port = int(CFG.get("listen_port", 8765))
    log.info("Claude Bridge %s starting on 127.0.0.1:%d; folders: %s", VERSION, port,
             ", ".join(f"{r.name}={'rw' if r.writable else 'ro'}:{r.path}" for r in ROOTS.values()))
    for r in ROOTS.values():
        if not r.path.exists():
            log.warning("folder %s does not exist: %s", r.name, r.path)

    CHROME_LINK.start()  # only sets up its thread; Claude Code's Chrome server starts on the first Chrome call
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_config=None, access_log=False,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
