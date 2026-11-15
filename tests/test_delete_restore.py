"""delete_files moves files into the bridge's history (files and empty folders only, never through links, all or
nothing), restore_file puts them back, and history copies last history_days from when they were made.
Throwaway folders and made-up files; runs on any OS:

    uv run python -m unittest discover -s tests
"""

import os
import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_secret_files  # noqa: E402,F401  (sets up the throwaway config before server is imported)

import server  # noqa: E402
from server import BridgeError  # noqa: E402

TMP = test_secret_files.TMP


class DeleteRestoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = TMP / "Notes" / "Clean"
        self.dir.mkdir()
        for name, text in (("a.md", "alpha"), ("b.md", "beta"), ("old.secret.txt", "made-up-old-token")):
            (self.dir / name).write_text(text, encoding="utf-8")
        shutil.rmtree(server.HISTORY_DIR, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_delete_keeps_a_copy_and_restore_puts_it_back(self):
        out = server.delete_files(["Notes/Clean/a.md"], reason="Raph asked: test")
        self.assertIn("Deleted 1", out)
        self.assertIn("restore_file", out)
        self.assertFalse((self.dir / "a.md").exists())
        listing = server.restore_file("Notes/Clean/a.md")
        self.assertIn("deleted copy", listing)
        self.assertNotIn("alpha", listing)  # versions only, never contents
        version = next(line[2:].split()[0] for line in listing.splitlines() if line.startswith("- "))
        server.restore_file("Notes/Clean/a.md", version=version)
        self.assertEqual((self.dir / "a.md").read_text(encoding="utf-8"), "alpha")

    def test_restore_over_an_existing_file_keeps_it_too(self):
        server.edit_text("Notes/Clean/b.md", "beta", "beta 2")
        version = server.history_copies(*server.resolve("Notes/Clean/b.md")[:2])[0][0]
        server.restore_file("Notes/Clean/b.md", version=version)
        self.assertEqual((self.dir / "b.md").read_text(encoding="utf-8"), "beta")
        kept = [f.read_text(encoding="utf-8") for _, f in server.history_copies(*server.resolve("Notes/Clean/b.md")[:2])]
        self.assertIn("beta 2", kept)  # the restore itself can be undone

    def test_secret_files_can_be_deleted_without_being_shown(self):
        out = server.delete_files(["Notes/Clean/old.secret.txt"], reason="Raph asked: old token")
        self.assertNotIn("made-up-old-token", out)
        listing = server.restore_file("Notes/Clean/old.secret.txt")
        self.assertIn(server.SECRET_TAG, listing)
        self.assertNotIn("made-up-old-token", listing)

    def test_folders_only_when_empty_and_all_or_nothing(self):
        with self.assertRaises(BridgeError) as cm:
            server.delete_files(["Notes/Clean/a.md", "Notes/Clean"], reason="test")  # b.md still in it
        self.assertIn("isn't empty", str(cm.exception))
        self.assertTrue((self.dir / "a.md").exists())  # nothing was done
        everything = ["Notes/Clean/a.md", "Notes/Clean/b.md", "Notes/Clean/old.secret.txt", "Notes/Clean"]
        server.delete_files(everything, reason="Raph asked: clear the folder")
        self.assertFalse(self.dir.exists())

    def test_refusals(self):
        with self.assertRaises(BridgeError):
            server.delete_files(["Notes/Clean/a.md"], reason="  ")  # a reason is required
        with self.assertRaises(BridgeError):
            server.delete_files(["Notes"], reason="test")  # a shared folder itself
        with self.assertRaises(BridgeError):
            server.delete_files(["Notes/Clean/missing.md"], reason="test")
        with self.assertRaises(BridgeError):
            server.delete_files([f"Notes/Clean/{i}.md" for i in range(server.DELETE_MAX + 1)], reason="test")
        (self.dir / "hidden.key").write_text("x", encoding="utf-8")
        with self.assertRaises(BridgeError):
            server.delete_files(["Notes/Clean/hidden.key"], reason="test")  # deny-listed files stay out of reach
        with self.assertRaises(BridgeError):  # ...and so do folders holding them
            server.delete_files(["Notes/Clean/a.md", "Notes/Clean/b.md", "Notes/Clean/old.secret.txt", "Notes/Clean"],
                                reason="test")
        self.assertTrue((self.dir / "a.md").exists())

    def test_never_deletes_through_a_link(self):
        try:
            (self.dir / "link.md").symlink_to(self.dir / "a.md")
        except (OSError, NotImplementedError):
            self.skipTest("can't make symlinks here")
        with self.assertRaises(BridgeError) as cm:
            server.delete_files(["Notes/Clean/link.md"], reason="test")
        self.assertIn("link", str(cm.exception))
        self.assertTrue((self.dir / "a.md").exists())

    def test_marked_destructive(self):
        tool = server.mcp._tool_manager.get_tool("delete_files")
        self.assertTrue(tool.annotations.destructive_hint)

    def test_history_copies_of_old_files_survive_pruning(self):
        old = self.dir / "a.md"
        os.utime(old, (time.time() - 90 * 86400,) * 2)  # last edited 90 days ago
        server.edit_text("Notes/Clean/a.md", "alpha", "alpha 2")
        server.delete_files(["Notes/Clean/b.md"], reason="test")
        os.utime(self.dir / "old.secret.txt", (time.time() - 90 * 86400,) * 2)
        server.prune_history()
        self.assertEqual(len(server.history_copies(*server.resolve("Notes/Clean/a.md")[:2])), 1)
        self.assertEqual(len(server.history_copies(*server.resolve("Notes/Clean/b.md", must_exist=False)[:2])), 1)


if __name__ == "__main__":
    unittest.main()
