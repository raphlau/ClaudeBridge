"""Secret files ('.secret' in the name) are link-only: listed, fetched or replaced through links, never read,
searched, edited or attached by a tool. Runs on any OS against a throwaway config and made-up files:

    uv run python -m unittest discover -s tests
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="bridge-test-"))
(TMP / "Files").mkdir()
(TMP / "Notes").mkdir()
(TMP / "config.json").write_text(json.dumps({
    "listen_port": 8765, "public_url": "https://pc.example.ts.net",
    "token": "Abc123Def456Ghi789Jkl012Mno345Pqr678Stu901V",
    "roots": [{"name": "Files", "path": str(TMP / "Files")}, {"name": "Notes", "path": str(TMP / "Notes")}],
    "log_dir": str(TMP / "logs"), "history_dir": str(TMP / "history"), "quiet": True,
}), encoding="utf-8")
os.environ["CLAUDE_BRIDGE_CONFIG"] = str(TMP / "config.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from server import BridgeError  # noqa: E402

FAKE = "made-up-token-0123456789"


class SecretFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = TMP / "Files" / "Money"
        self.dir.mkdir(exist_ok=True)
        (self.dir / "api.secret.txt").write_text(FAKE, encoding="utf-8")
        (self.dir / "plain.txt").write_text("nothing to hide " + FAKE, encoding="utf-8")
        server.LINKS.clear()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def assertRefusedWithHelp(self, call, *args, **kw):
        with self.assertRaises(BridgeError) as cm:
            call(*args, **kw)
        msg = str(cm.exception)
        self.assertIn("secret file", msg)
        self.assertNotIn(FAKE, msg)
        return msg

    # -- which names count
    def test_names(self):
        for name in ("synci-token.secret.txt", "api.secret", "API.SECRET.TXT", "Files/x/key.secret.json"):
            self.assertTrue(server.is_secret(name), name)
        for name in ("secretary.txt", "mysecret.txt", "a.secrets.txt", "secret.txt", "Secret notes.md"):
            self.assertFalse(server.is_secret(name), name)

    # -- visible, and marked
    def test_listed_and_marked(self):
        out = server.list_dir("Files/Money")
        self.assertIn("api.secret.txt", out)
        line = next(x for x in out.splitlines() if "api.secret.txt" in x)
        self.assertIn(server.SECRET_TAG, line)
        self.assertNotIn(server.SECRET_TAG, next(x for x in out.splitlines() if "plain.txt" in x))
        self.assertIn(server.SECRET_TAG, server.list_dir("Files", recursive=True))
        self.assertIn(server.SECRET_TAG, server.list_dir("Files/Money/api.secret.txt"))
        self.assertIn(server.SECRET_TAG, server.find_files("api"))

    # -- nothing returns or probes the contents
    def test_read_tools_refuse_and_explain(self):
        msg = self.assertRefusedWithHelp(server.read_text, "Files/Money/api.secret.txt")
        for hint in ("create_download_link", "private file", "Never print", "command-line argument", "another route"):
            self.assertIn(hint, msg)
        (self.dir / "doc.secret.pdf").write_bytes(b"not really a pdf")
        (self.dir / "pic.secret.png").write_bytes(b"not really a png")
        self.assertRefusedWithHelp(server.read_pdf, "Files/Money/doc.secret.pdf")
        self.assertRefusedWithHelp(server.view_file, "Files/Money/pic.secret.png")

    def test_innocent_link_to_a_secret_is_refused(self):
        try:
            (self.dir / "harmless.txt").symlink_to(self.dir / "api.secret.txt")
        except (OSError, NotImplementedError):
            self.skipTest("can't make symlinks here")
        self.assertRefusedWithHelp(server.read_text, "Files/Money/harmless.txt")

    def test_search_skips_secrets(self):
        out = server.search_text(FAKE)
        self.assertIn("plain.txt", out)
        self.assertNotIn("api.secret.txt", out)
        self.assertRefusedWithHelp(server.search_text, FAKE, path="Files/Money/api.secret.txt")

    def test_edit_is_refused_whether_or_not_the_text_matches(self):
        a = self.assertRefusedWithHelp(server.edit_text, "Files/Money/api.secret.txt", FAKE, "x")
        b = self.assertRefusedWithHelp(server.edit_text, "Files/Money/api.secret.txt", "wrong guess", "x")
        self.assertEqual(a, b)  # no oracle
        self.assertEqual((self.dir / "api.secret.txt").read_text(encoding="utf-8"), FAKE)

    def test_writes_are_refused(self):
        msg = self.assertRefusedWithHelp(server.write_text, "Files/Money/new.secret.txt", "x")
        self.assertIn("create_upload_link", msg)
        self.assertIn("tell Raph", msg)
        self.assertFalse((self.dir / "new.secret.txt").exists())
        self.assertRefusedWithHelp(server.write_text, "Files/Money/api.secret.txt", "x", overwrite=True)
        self.assertRefusedWithHelp(server.append_text, "Files/Money/api.secret.txt", "x")
        self.assertEqual((self.dir / "api.secret.txt").read_text(encoding="utf-8"), FAKE)

    def test_not_attachable_to_web_pages(self):
        self.assertRefusedWithHelp(server._upload_files, ["Files/Money/plain.txt", "Files/Money/api.secret.txt"])
        self.assertEqual(len(server._upload_files(["Files/Money/plain.txt"])), 1)

    # -- links work, and a secret's download link works once, briefly
    def test_download_link_is_one_use_and_short(self):
        out = server.create_download_link("Files/Money/api.secret.txt", minutes=60)
        self.assertIn("works once", out)
        self.assertIn("private file", out)
        tok = out.splitlines()[0].rsplit("/", 1)[1]
        info = server.LINKS[tok]
        self.assertTrue(info["once"])
        self.assertLessEqual(info["expires"] - time.time(), server.SECRET_LINK_MINUTES * 60 + 1)
        plain = server.create_download_link("Files/Money/plain.txt", minutes=60)
        self.assertFalse(server.LINKS[plain.splitlines()[0].rsplit("/", 1)[1]]["once"])

    def test_download_and_upload_over_http(self):
        from starlette.testclient import TestClient

        client = TestClient(server.build_app())
        url = server.create_download_link("Files/Money/api.secret.txt").splitlines()[0]
        path = url.split("ts.net", 1)[1]
        self.assertEqual(client.get(path).text, FAKE)
        try:
            again = client.get(path).status_code
        except Exception:  # the gate may just drop the connection
            again = None
        self.assertNotEqual(again, 200)

        plain = server.create_download_link("Files/Money/plain.txt").splitlines()[0].split("ts.net", 1)[1]
        self.assertEqual(client.get(plain).status_code, 200)
        self.assertEqual(client.get(plain).status_code, 200)  # ordinary links stay reusable until they expire

        up = server.create_upload_link("Files/Money/api.secret.txt", overwrite=True)
        self.assertIn("private file", up)
        r = client.put(up.splitlines()[0].split("ts.net", 1)[1], content=b"renewed-made-up-token")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.dir / "api.secret.txt").read_bytes(), b"renewed-made-up-token")

    # -- the deny list still wins
    def test_denied_files_stay_hidden(self):
        (self.dir / "id.key").write_text("x", encoding="utf-8")
        self.assertNotIn("id.key", server.list_dir("Files/Money"))
        with self.assertRaises(BridgeError):
            server.create_download_link("Files/Money/id.key")

    def test_instructions_mention_secrets(self):
        self.assertIn(".secret", server.INSTRUCTIONS)
        self.assertIn(server.SECRET_TAG, server.INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
