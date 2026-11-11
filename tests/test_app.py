"""App server tests: auth, the event stream, a full task with a permission prompt, stop, and settings."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ.setdefault("OMNI_STATE", tempfile.mkdtemp(prefix="omni-state-"))

from omni import builtin, memory                          # noqa: E402
from omni.app import Session, make_handler                # noqa: E402
from omni.core import Config                              # noqa: E402
from tests.test_offline import Script, call, reply        # noqa: E402

TOKEN = "test-token"


class FakeBrowser:
    """Stands in for omni.browser.Browser (no Playwright): records the panel's input."""

    def __init__(self):
        self.mode, self.cdp_url, self.started = "agent", "http://127.0.0.1:9222", False
        self.frame, self.frame_seq, self.frame_info, self.on_frame, self.calls = None, 0, {}, [], []

    def user_input(self, action, **a):
        self.calls.append((action, a))
        self.started = True
        if action == "navigate":
            self.frame_info = {"url": "https://example.com/", "title": "Example", "width": 1280, "height": 860}
        return self.capture()

    def capture(self, quiet=False):
        self.frame, self.frame_seq = b"\xff\xd8jpeg", self.frame_seq + 1
        for cb in self.on_frame:
            cb(self.frame_seq, self.frame_info)
        return self.frame_seq

    def set_mode(self, mode, cdp_url=None):
        self.mode, self.started = mode, False

    def ensure(self):
        self.started = True


class AppTest(unittest.TestCase):
    def start(self, script, mode="ask"):
        self.ws = Path(tempfile.mkdtemp(prefix="omni-appws-"))
        cfg = Config(workspace=str(self.ws), mode=mode)
        self.session = Session(cfg, [], chat_fn=script, modules=[builtin, memory])
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = self.srv.server_address[1]
        self.srv.RequestHandlerClass = make_handler(self.session, TOKEN, port)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.session.stop()
        shutil.rmtree(self.ws, ignore_errors=True)

    def req(self, path, body=None, token=TOKEN):
        r = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                    headers={"X-Omni-Token": token, "Content-Type": "application/json"})
        try:
            with urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except HTTPError as e:
            return e.code, {}

    def wait_for(self, kind, timeout=10, **match):
        end = time.time() + timeout
        while time.time() < end:
            for ev in self.session.hub.events:
                if ev["kind"] == kind and all(ev.get(k) == v for k, v in match.items()):
                    return ev
            time.sleep(0.05)
        self.fail(f"no {kind} event; got {[e['kind'] for e in self.session.hub.events]}")

    def test_token_and_host_required(self):
        self.start(Script())
        self.assertEqual(self.req("/api/state", token="wrong")[0], 403)
        self.assertEqual(self.req("/api/send", {"text": "hi"}, token="")[0], 403)
        r = Request(self.base + "/api/state", headers={"X-Omni-Token": TOKEN, "Host": "evil.example:80"})
        with self.assertRaises(HTTPError) as cm:
            urlopen(r, timeout=5)
        self.assertEqual(cm.exception.code, 403)
        code, st = self.req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(st["mode"], "ask")
        with urlopen(Request(self.base + f"/?t={TOKEN}"), timeout=5) as resp:
            page = resp.read().decode()
        self.assertIn(TOKEN, page)
        self.assertNotIn("__OMNI_TOKEN__", page)

    def test_task_with_permission_prompt(self):
        s = Script(reply(json.dumps({"steps": ["Write note.txt with the text hello"]})),
                   call("write_file", path="note.txt", content="hello"), reply("Wrote note.txt"))
        self.start(s, mode="ask")
        self.assertEqual(self.req("/api/send", {"text": "make a note"})[0], 200)
        self.assertEqual(self.req("/api/send", {"text": "another"})[0], 409)        # busy
        conf = self.wait_for("confirm")
        self.assertIn("write_file", conf["text"])
        self.assertEqual(self.req("/api/confirm", {"id": conf["id"], "allow": True})[0], 200)
        res = self.wait_for("result")
        self.assertEqual(res["notes"], ["Wrote note.txt"])
        self.wait_for("busy", busy=False)
        self.assertEqual((self.ws / "note.txt").read_text(), "hello")
        kinds = [e["kind"] for e in self.session.hub.events]
        for k in ("user", "planning", "plan", "step_start", "tool_call", "confirm", "confirm_answer", "tool_result", "step_done"):
            self.assertIn(k, kinds)
        self.assertFalse(any(e["kind"] == "log" and "> write_file" in e.get("text", "") for e in self.session.hub.events))

    def test_stop_denies_pending_confirmation(self):
        s = Script(reply(json.dumps({"steps": ["Write x.txt with the text x"]})),
                   call("write_file", path="x.txt", content="x"), reply("never reached"))
        self.start(s, mode="ask")
        self.req("/api/send", {"text": "go"})
        self.wait_for("confirm")
        self.req("/api/stop", {})
        self.wait_for("busy", busy=False)
        self.assertFalse((self.ws / "x.txt").exists())
        self.assertTrue(any(e["kind"] in ("stopped", "confirm_answer") for e in self.session.hub.events))

    def test_slash_command_and_settings(self):
        self.start(Script(), mode="auto")
        self.req("/api/send", {"text": "/help"})
        ev = self.wait_for("command_result")
        self.assertIn("/improve", ev["text"])
        self.wait_for("busy", busy=False)
        code, _ = self.req("/api/settings", {"mode": "yolo", "plan": False})
        self.assertEqual(code, 200)
        self.assertEqual(self.session.agent.mode, "yolo")
        self.assertFalse(self.session.agent.cfg.use_plan)
        self.assertEqual(self.req("/api/settings", {"workspace": "Z:/definitely/missing"})[0], 409)

    def test_always_allow_from_the_card(self):
        s = Script(reply(json.dumps({"steps": ["Write two files"]})),
                   call("write_file", path="1.txt", content="1"), call("write_file", path="2.txt", content="2"),
                   reply("Wrote both"))
        self.start(s, mode="ask")
        self.req("/api/send", {"text": "write two files"})
        conf = self.wait_for("confirm")
        self.assertEqual(conf["always_label"], "Always allow file writes")
        self.req("/api/confirm", {"id": conf["id"], "allow": True, "always": True})
        self.wait_for("result")
        self.assertEqual([e["kind"] for e in self.session.hub.events].count("confirm"), 1)   # 2nd write didn't ask
        self.assertTrue((self.ws / "2.txt").exists())
        self.assertEqual(self.req("/api/state")[1]["allowed"][0]["key"], "write_file")
        self.req("/api/allowed", {})
        self.assertEqual(self.session.agent.session_allow, set())

    def test_agent_question_answered_in_app(self):
        s = Script(reply(json.dumps({"steps": ["Write name.txt with the user's name"]})),
                   call("ask_user", question="What name?"), call("write_file", path="name.txt", content="Asha"),
                   reply("Wrote name.txt"))
        self.start(s, mode="yolo")
        self.req("/api/send", {"text": "write my name to name.txt"})
        q = self.wait_for("question")
        self.assertEqual(q["text"], "What name?")
        self.req("/api/answer", {"id": q["id"], "text": "Asha"})
        self.wait_for("result")
        tool_out = next(e for e in self.session.hub.events if e["kind"] == "tool_result" and e["name"] == "ask_user")
        self.assertIn("Asha", tool_out["output"])

    def test_scheduled_task_runs(self):
        from omni import schedule
        s = Script(reply(json.dumps({"steps": ["Write tick.txt"]})), call("write_file", path="tick.txt", content="t"),
                   reply("ok"))
        self.start(s, mode="yolo")
        a = self.session.agent
        a.schedules = schedule.Schedules(self.ws / "sched.json")
        import datetime as dt
        it = a.schedules.add("write tick.txt", "every 30m", str(self.ws), now=dt.datetime.now() - dt.timedelta(hours=1))
        self.assertTrue(self.session.run_due())
        user = self.wait_for("user")
        self.assertEqual(user["schedule"]["id"], it["id"])
        self.wait_for("busy", busy=False)
        saved = a.schedules.load()[0]
        self.assertEqual(saved["last_status"], "none")
        self.assertGreater(dt.datetime.fromisoformat(saved["next_run"]), dt.datetime.now())
        self.assertTrue((self.ws / "tick.txt").exists())
        self.assertFalse(self.session.run_due())                        # not due again yet

    def test_event_fields_cannot_overwrite_stream_position(self):
        self.start(Script())
        self.session.hub.push("log", text="a")
        self.session.hub.push("browser_update", seq=1, ts=0, frame=7)
        ev = self.session.hub.events[-1]
        self.assertEqual((ev["seq"], ev["kind"], ev["frame"]), (2, "browser_update", 7))

    def test_browser_panel_endpoints(self):
        self.start(Script())
        fake = FakeBrowser()
        self.session.agent.browser = fake
        fake.on_frame.append(lambda seq, info: self.session.hub.push("browser_update", frame=seq, **info))
        self.assertEqual(self.req("/api/browser/input", {"action": "click", "x": 1, "y": 2})[0], 409)  # no page yet
        code, r = self.req("/api/browser/input", {"action": "navigate", "url": "example.com"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["browser"]["url"], "https://example.com/")
        code, r = self.req("/api/browser/input", {"action": "type", "text": "secret-password-123"})
        self.assertEqual(code, 200)
        self.assertEqual(fake.calls[-1], ("type", {"text": "secret-password-123"}))
        self.assertNotIn("secret-password-123", json.dumps(self.session.hub.events))   # typing is never logged
        upd = self.wait_for("browser_update")
        self.assertEqual(upd["frame"], 1)
        with urlopen(Request(self.base + f"/api/browser/frame?t={TOKEN}"), timeout=5) as resp:
            self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
            self.assertEqual(resp.read(), b"\xff\xd8jpeg")
        self.assertEqual(self.req("/api/state")[1]["browser"]["mode"], "agent")
        code, r = self.req("/api/browser/mode", {"mode": "attach"})
        self.assertEqual(code, 200, r)
        self.assertEqual(fake.mode, "attach")
        self.assertEqual(self.req("/api/browser/mode", {"mode": "bogus"})[0], 409)

    def test_frame_endpoint_without_browser_page(self):
        self.start(Script())
        self.session.agent.browser = FakeBrowser()
        with urlopen(Request(self.base + f"/api/browser/frame?t={TOKEN}"), timeout=5) as resp:
            self.assertEqual(resp.status, 204)

    def test_event_stream_replays_from_start(self):
        self.start(Script())
        self.session.hub.push("log", text="one")
        with urlopen(Request(self.base + f"/api/events?t={TOKEN}&since=0"), timeout=5) as resp:
            got = b""
            while b"one" not in got:
                got += resp.readline()
        self.assertIn(b"event: hello", got)


if __name__ == "__main__":
    unittest.main()
