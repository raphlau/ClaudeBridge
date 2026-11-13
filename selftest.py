"""Self-test for Claude Bridge. Run with the bridge's own Python:
    .venv\\Scripts\\python.exe selftest.py            (local, http://127.0.0.1:<port>)
    .venv\\Scripts\\python.exe selftest.py --public   (also through Tailscale Funnel)
Read-only: it lists folders, searches, checks which apps (MEGA, Obsidian, Chrome, Claude) are running, and confirms
that requests without the token are refused. It changes nothing.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

CFG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8-sig"))
TOKEN = CFG["token"]


def text_of(res) -> str:
    return "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")


async def check(base: str) -> bool:
    print(f"\n## {base}")
    ok = True
    async with httpx2.AsyncClient(timeout=30) as hc:
        r = await hc.get(f"{base}/healthz")
        print(f"healthz: {r.status_code} {r.text!r}")
        r = await hc.post(f"{base}/mcp", json={})
        refused = r.status_code == 401
        ok &= refused
        print(f"no token refused: {'yes' if refused else 'NO (status %d)' % r.status_code}")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60) as hc:
        async with streamable_http_client(f"{base}/mcp", http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                tools = [t.name for t in (await s.list_tools()).tools]
                print(f"tools ({len(tools)}): {', '.join(tools)}")
                for name, args in [("list_dir", {}), ("app_status", {})]:
                    res = await s.call_tool(name, args)
                    ok &= not res.is_error
                    print(f"\n{name}{' [ERROR]' if res.is_error else ''}:\n{text_of(res)}")
                for root in [r["name"] for r in CFG["roots"]]:
                    res = await s.call_tool("list_dir", {"path": root, "max_entries": 8})
                    ok &= not res.is_error
                    print(f"\nlist_dir {root}{' [ERROR]' if res.is_error else ''}:\n{text_of(res)}")
                res = await s.call_tool("find_files", {"pattern": "*.md", "max_results": 3})
                print(f"\nfind_files *.md:\n{text_of(res)}")
    return ok


async def main():
    bases = [f"http://127.0.0.1:{CFG.get('listen_port', 8765)}"]
    if "--public" in sys.argv:
        bases.append(CFG["public_url"].rstrip("/"))
    results = []
    for b in bases:
        try:
            results.append(await check(b))
        except Exception as e:  # noqa: BLE001
            print(f"FAILED: {type(e).__name__}: {e}")
            results.append(False)
    print("\nSELFTEST", "PASSED" if all(results) else "FAILED")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
