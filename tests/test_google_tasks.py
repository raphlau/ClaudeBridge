"""Google Tasks: the sign-in (PKCE, finished by pasting the address Google redirected to), the saved sign-in in a
secret file, and the task tools. Talks to a fake Google, never the real one; runs on any OS:

    uv run python -m unittest discover -s tests
"""

import base64
import hashlib
import io
import json
import shutil
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_secret_files  # noqa: E402,F401  (sets up the throwaway config before server is imported)

import server  # noqa: E402
from server import BridgeError  # noqa: E402

TMP = test_secret_files.TMP
SECRET = TMP / "Files" / "Claude" / "google-tasks.secret.json"
CLIENT_SECRET = "made-up-client-secret-777"
REFRESH = "made-up-refresh-token-888"
ACCESS = "made-up-access-token-999"
CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": CLIENT_SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGoogle:
    """Just enough of Google's token endpoint and the Tasks API."""

    def __init__(self):
        self.calls = []
        self.challenge = ""
        self.refresh_ok = True
        self.disabled = False
        self.lists = [{"id": "L1", "title": "My Tasks", "updated": "2026-11-19T10:00:00Z"},
                      {"id": "L2", "title": "Shopping", "updated": "2026-11-18T10:00:00Z"}]
        self.tasks = {
            "L1": [{"id": "t1", "title": "Buy milk", "status": "needsAction", "due": "2026-11-20T00:00:00.000Z",
                    "position": "001", "notes": "semi-skimmed\nfrom the corner shop"},
                   {"id": "t2", "title": "Two pints", "status": "needsAction", "parent": "t1", "position": "000"},
                   {"id": "t3", "title": "Old thing", "status": "completed", "completed": "2026-11-18T09:00:00Z",
                    "position": "002", "hidden": True}],
            "L2": [{"id": "t4", "title": "Eggs", "status": "needsAction", "position": "000"}],
        }
        self.next_id = 10

    @staticmethod
    def _json(obj, status=200):
        return FakeResponse(json.dumps(obj).encode()) if status == 200 else obj

    def _error(self, url, status, info):
        raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(json.dumps(info).encode()))

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        body = req.data.decode() if req.data else ""
        self.calls.append({"method": method, "url": url, "path": parts.path, "query": query, "body": body,
                           "auth": req.get_header("Authorization", "")})
        if parts.netloc == "oauth2.googleapis.com" and parts.path == "/token":
            form = dict(urllib.parse.parse_qsl(body))
            if form.get("client_secret") != CLIENT_SECRET:
                self._error(url, 401, {"error": "invalid_client"})
            if form["grant_type"] == "authorization_code":
                verifier = form.get("code_verifier", "")
                s256 = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if form.get("code") != "4/good-code" or s256 != self.challenge or form["redirect_uri"] != server.GT_REDIRECT:
                    self._error(url, 400, {"error": "invalid_grant", "error_description": "Bad Request"})
                return self._json({"access_token": ACCESS, "expires_in": 3599, "refresh_token": REFRESH,
                                   "scope": server.GT_SCOPE, "token_type": "Bearer"})
            if form["grant_type"] == "refresh_token":
                if not self.refresh_ok or form.get("refresh_token") != REFRESH:
                    self._error(url, 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
                return self._json({"access_token": ACCESS, "expires_in": 3599, "token_type": "Bearer"})
        if parts.netloc == "oauth2.googleapis.com" and parts.path == "/revoke":
            return self._json({})
        assert parts.netloc == "tasks.googleapis.com", url
        if req.get_header("Authorization") != f"Bearer {ACCESS}":
            self._error(url, 401, {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}})
        if self.disabled:
            self._error(url, 403, {"error": {"code": 403, "message": "Google Tasks API has not been used in project 1",
                                             "errors": [{"reason": "accessNotConfigured"}], "status": "PERMISSION_DENIED"}})
        path = parts.path.removeprefix("/tasks/v1")
        if path == "/users/@me/lists":
            return self._json({"items": self.lists})
        if path == "/users/@me/lists/@default":
            return self._json(self.lists[0])
        seg = path.strip("/").split("/")  # lists/<L>/tasks[/<T>]
        lst = self.tasks.get(seg[1])
        if lst is None:
            self._error(url, 404, {"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}})
        if len(seg) == 3 and method == "GET":
            show_done = query.get("showCompleted", "true") == "true" and query.get("showHidden") == "true"
            items = [t for t in lst if show_done or t["status"] != "completed"]
            return self._json({"items": items})
        if len(seg) == 3 and method == "POST":
            t = {"id": f"t{self.next_id}", "status": "needsAction", **json.loads(body)}
            if query.get("parent"):
                t["parent"] = query["parent"]
            self.next_id += 1
            lst.append(t)
            return self._json(t)
        task = next((t for t in lst if t["id"] == seg[3]), None)
        if task is None:
            self._error(url, 404, {"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}})
        if method == "GET":
            return self._json(task)
        if method == "PATCH":
            for k, v in json.loads(body).items():
                if v is None:
                    task.pop(k, None)
                else:
                    task[k] = v
            return self._json(task)
        if method == "DELETE":
            lst.remove(task)
            return FakeResponse(b"")
        raise AssertionError(f"unexpected {method} {url}")

    def last(self, method, path_part):
        return next(c for c in reversed(self.calls) if c["method"] == method and path_part in c["path"])


class GoogleTasksTests(unittest.TestCase):
    def setUp(self):
        self.google = FakeGoogle()
        patcher = mock.patch.object(server.urllib.request, "urlopen", self.google)
        patcher.start()
        self.addCleanup(patcher.stop)
        server._gt_access.clear()
        server._gt_pending.clear()
        shutil.rmtree(SECRET.parent, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(SECRET.parent, ignore_errors=True)

    def write_secret(self, **extra):
        SECRET.parent.mkdir(parents=True, exist_ok=True)
        SECRET.write_text(json.dumps({**CLIENT, **extra}), encoding="utf-8")

    def connected(self):
        self.write_secret(refresh_token=REFRESH, scope=server.GT_SCOPE, connected_at="2026-11-19 18:00")

    def assertNoSecrets(self, text):
        for s in (CLIENT_SECRET, REFRESH, ACCESS):
            self.assertNotIn(s, text)

    def start_sign_in(self):
        out = server.connect_google_tasks()
        url = next(line for line in out.splitlines() if line.startswith(server.GT_AUTH_URL))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.google.challenge = q["code_challenge"]
        return out, q

    # -- before it's set up
    def test_not_set_up_explains_the_steps(self):
        for call in (server.connect_google_tasks, server.list_tasks, server.list_task_lists):
            with self.assertRaises(BridgeError) as cm:
                call()
            msg = str(cm.exception)
            for hint in ("Google Tasks API", "Desktop app", "Publish app", "Files/Claude/google-tasks.secret.json"):
                self.assertIn(hint, msg)
        self.assertEqual(self.google.calls, [])

    def test_wrong_kind_of_client_is_refused_without_showing_it(self):
        SECRET.parent.mkdir(parents=True)
        SECRET.write_text(json.dumps({"web": {"client_id": "x", "client_secret": CLIENT_SECRET}}), encoding="utf-8")
        with self.assertRaises(BridgeError) as cm:
            server.connect_google_tasks()
        self.assertIn("Web application", str(cm.exception))
        self.assertNoSecrets(str(cm.exception))

    # -- signing in
    def test_sign_in_from_any_device_by_pasting_the_address(self):
        self.write_secret()
        out, q = self.start_sign_in()
        self.assertNoSecrets(out)
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:8766")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual((q["access_type"], q["prompt"], q["scope"]), ("offline", "consent", server.GT_SCOPE))
        self.assertNotIn("client_secret", q)
        pasted = (f"http://127.0.0.1:8766/?state={q['state']}&iss=https://accounts.google.com&code=4/good-code"
                  f"&scope={urllib.parse.quote(server.GT_SCOPE)}")
        done = server.connect_google_tasks(redirect_url=pasted)
        self.assertIn("Connected to Google Tasks", done)
        self.assertIn("My Tasks, Shopping", done)
        self.assertNoSecrets(done)
        saved = json.loads(SECRET.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], REFRESH)
        self.assertEqual(saved["installed"], CLIENT["installed"])  # the client stays as Google gave it
        # the file stays link-only for every other tool
        with self.assertRaises(BridgeError):
            server.read_text("Files/Claude/google-tasks.secret.json")
        self.assertIn("is connected since", server.connect_google_tasks())

    def test_a_pasted_address_works_once_and_only_for_the_latest_link(self):
        self.write_secret()
        _, q = self.start_sign_in()
        with self.assertRaises(BridgeError) as cm:
            server.connect_google_tasks(redirect_url=f"http://127.0.0.1:8766/?state=other&code=4/good-code")
        self.assertIn("older sign-in link", str(cm.exception))
        with self.assertRaises(BridgeError) as cm:  # the link was used up by that attempt
            server.connect_google_tasks(redirect_url=f"http://127.0.0.1:8766/?state={q['state']}&code=4/good-code")
        self.assertIn("no sign-in waiting", str(cm.exception))
        self.assertNotIn("refresh_token", json.loads(SECRET.read_text(encoding="utf-8")))

    def test_declined_or_bad_code(self):
        self.write_secret()
        _, q = self.start_sign_in()
        out = server.connect_google_tasks(redirect_url=f"http://127.0.0.1:8766/?error=access_denied&state={q['state']}")
        self.assertIn("didn't allow access", out)
        _, q = self.start_sign_in()
        with self.assertRaises(BridgeError) as cm:
            server.connect_google_tasks(redirect_url=f"http://127.0.0.1:8766/?state={q['state']}&code=4/stale-code")
        self.assertIn("didn't accept that code", str(cm.exception))
        with self.assertRaises(BridgeError):
            server.connect_google_tasks(redirect_url="hello there")

    def test_expired_sign_in_says_how_to_fix_it(self):
        self.connected()
        self.google.refresh_ok = False
        with self.assertRaises(BridgeError) as cm:
            server.list_tasks()
        msg = str(cm.exception)
        for hint in ("7 days", "Publish app", "reconnect=True"):
            self.assertIn(hint, msg)
        out = server.connect_google_tasks()  # offers a new link straight away
        self.assertIn("refused the saved sign-in", out)
        self.assertIn(server.GT_AUTH_URL, out)

    def test_a_stale_access_token_is_refreshed_once(self):
        self.connected()
        server._gt_access.update(token="stale", expires=server.time.time() + 3000)
        self.assertIn("My Tasks", server.list_task_lists())
        self.assertEqual(sum(1 for c in self.google.calls if c["path"] == "/token"), 1)

    def test_api_switched_off(self):
        self.connected()
        self.google.disabled = True
        with self.assertRaises(BridgeError) as cm:
            server.list_task_lists()
        self.assertIn("enable 'Google Tasks API'", str(cm.exception))

    def test_disconnect_revokes_and_forgets(self):
        self.connected()
        out = server.connect_google_tasks(disconnect=True)
        self.assertIn("revoked at Google", out)
        self.assertNoSecrets(out)
        self.assertEqual(urllib.parse.parse_qs(self.google.last("POST", "/revoke")["body"])["token"], [REFRESH])
        saved = json.loads(SECRET.read_text(encoding="utf-8"))
        self.assertNotIn("refresh_token", saved)
        self.assertIn("installed", saved)

    # -- the task tools
    def test_list_tasks(self):
        self.connected()
        out = server.list_tasks()
        self.assertIn("My Tasks (list id: L1):", out)
        self.assertIn("- [ ] Buy milk  (due 2026-11-20, id: t1)", out)
        self.assertIn("    notes: semi-skimmed / from the corner shop", out)
        self.assertIn("  - [ ] Two pints  (id: t2)", out)  # subtask under its parent
        self.assertIn("Shopping (list id: L2):", out)
        self.assertNotIn("Old thing", out)
        self.assertEqual(self.google.last("GET", "/lists/L1/tasks")["query"]["showCompleted"], "false")
        out = server.list_tasks(tasklist="my tasks", include_completed=True)
        self.assertIn("- [x] Old thing  (done 2026-11-18, id: t3)", out)
        self.assertNotIn("Shopping", out)
        server.list_tasks(due_from="2026-11-20", due_to="2026-11-21")
        q = self.google.last("GET", "/lists/L2/tasks")["query"]
        self.assertEqual((q["dueMin"], q["dueMax"]), ("2026-11-19T23:59:59.999Z", "2026-11-21T23:59:59.999Z"))
        with self.assertRaises(BridgeError) as cm:
            server.list_tasks(tasklist="Work")
        self.assertIn("'My Tasks', 'Shopping'", str(cm.exception))

    def test_add_task(self):
        self.connected()
        out = server.add_task("Ring Etihad about the claim", tasklist="shopping", due="2026-11-21T15:00")
        self.assertIn("Added to 'Shopping', due 2026-11-21", out)
        self.assertIn("time was dropped", out)
        self.assertEqual(json.loads(self.google.last("POST", "/lists/L2/tasks")["body"]),
                         {"title": "Ring Etihad about the claim", "due": "2026-11-21T00:00:00.000Z"})
        out = server.add_task("Oat milk", parent_id="t1")  # default list
        self.assertIn("Added to 'My Tasks'", out)
        self.assertEqual(self.google.last("POST", "/lists/L1/tasks")["query"], {"parent": "t1"})
        with self.assertRaises(BridgeError):
            server.add_task("x", due="next Tuesday")
        # titles and notes aren't logged
        log_text = (Path(server.LOG_DIR) / "bridge.log").read_text(encoding="utf-8")
        self.assertNotIn("Ring Etihad", log_text)

    def test_update_and_delete(self):
        self.connected()
        out = server.update_task("t4", done=True)  # found without naming the list
        self.assertIn("Updated 'Eggs' in 'Shopping': done", out)
        self.assertEqual(json.loads(self.google.last("PATCH", "/t4")["body"]), {"status": "completed"})
        server.update_task("t4", tasklist="Shopping", done=False, due="2026-11-25")
        self.assertEqual(json.loads(self.google.last("PATCH", "/t4")["body"]),
                         {"due": "2026-11-25T00:00:00.000Z", "status": "needsAction", "completed": None})
        out = server.update_task("t1", clear_due=True, notes="")
        self.assertIn("no due date", out)
        self.assertEqual(json.loads(self.google.last("PATCH", "/t1")["body"]), {"notes": "", "due": None})
        with self.assertRaises(BridgeError):
            server.update_task("t1")
        with self.assertRaises(BridgeError) as cm:
            server.update_task("nope", done=True)
        self.assertIn("No task with id 'nope'", str(cm.exception))
        with self.assertRaises(BridgeError):
            server.delete_task("t4", reason=" ")
        out = server.delete_task("t4", reason="Raph asked")
        self.assertIn("Deleted 'Eggs' from 'Shopping'", out)
        self.assertEqual(self.google.tasks["L2"], [])

    def test_tools_are_listed_with_annotations(self):
        tools = {t.name: t for t in server.asyncio.run(server.mcp.list_tools())}
        for name in ("connect_google_tasks", "list_task_lists", "list_tasks", "add_task", "update_task", "delete_task"):
            self.assertIn(name, tools)
        self.assertTrue(tools["list_tasks"].annotations.read_only_hint)
        self.assertTrue(tools["delete_task"].annotations.destructive_hint)
        self.assertFalse(tools["add_task"].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
