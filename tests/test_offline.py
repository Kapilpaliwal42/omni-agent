"""Offline tests with a scripted fake model: no Ollama, network, browser or desktop needed.

Run:  .venv\\Scripts\\python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_TMP_STATE = tempfile.mkdtemp(prefix="omni-state-")
os.environ["OMNI_STATE"] = _TMP_STATE           # must be set before omni is imported

from omni import builtin, memory, mcp, skills, selfupdate                     # noqa: E402
from omni.core import Agent, Config                                          # noqa: E402
from omni.desktop import escape_text, hotkey_to_keys                         # noqa: E402
from omni.registry import Tool                                               # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def reply(content="", calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in calls]
    return {"message": msg, "done_reason": "stop"}


def call(_tool, **args):
    return reply(json.dumps({"name": _tool, "arguments": args}))


class Script:
    """Fake model: returns scripted replies in order and records every payload it was sent.

    The "understand the request" and "check the requirements" calls have their own queues (routes, reviews),
    so tests that don't care about them script only the plan and the tool calls. By default a message is a
    task with no extracted requirements, which skips the requirement check."""

    def __init__(self, *replies, routes=None, reviews=None, rules=None):
        self.replies, self.payloads = list(replies), []
        self.routes, self.reviews, self.rules = list(routes or []), list(reviews or []), list(rules or [])

    def __call__(self, payload):
        self.payloads.append(payload)
        props = (payload.get("format") or {}).get("properties", {})
        if "kind" in props:
            r = self.routes.pop(0) if self.routes else {"kind": "task", "task": "", "requirements": [], "question": ""}
            return reply(json.dumps(r))
        if "results" in props:
            return reply(json.dumps(self.reviews.pop(0) if self.reviews else {"results": []}))
        if "rule" in props:
            return reply(json.dumps({"rule": self.rules.pop(0) if self.rules else ""}))
        if not self.replies:
            return reply("done")
        r = self.replies.pop(0)
        return r(payload) if callable(r) else r


def route(kind="task", task="", requirements=(), question=""):
    return {"kind": kind, "task": task, "requirements": list(requirements), "question": question}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def make_git_repo(d: Path):
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "test@example.invalid")
    git(d, "config", "user.name", "test")
    git(d, "config", "commit.gpgsign", "false")


class Base(unittest.TestCase):
    modules = [builtin, memory]

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="omni-ws-"))
        self.answers: list[str] = []
        self.printed: list[str] = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def agent(self, script, mode="auto", modules=None, **extra_cfg):
        extra = extra_cfg.pop("extra", {})
        cfg = Config(workspace=str(self.ws), mode=mode, use_plan=extra_cfg.pop("use_plan", True), extra=extra, **extra_cfg)
        return Agent(cfg, ui_input=lambda prompt: self.answers.pop(0) if self.answers else "n",
                     ui_print=lambda *a: self.printed.append(" ".join(map(str, a))),
                     chat_fn=script, modules=modules if modules is not None else self.modules)


class TestLoop(Base):
    def test_plan_then_step_writes_file(self):
        s = Script(reply(json.dumps({"steps": ["Write hello.txt containing the word hi"]})),
                   call("write_file", path="hello.txt", content="hi\n"),
                   reply("Wrote hello.txt"))
        res = self.agent(s).run("make hello.txt")
        self.assertEqual((self.ws / "hello.txt").read_text(), "hi\n")
        self.assertEqual(res["notes"], ["Wrote hello.txt"])
        self.assertEqual(res["status"], "none")                       # no tests in the workspace

    def test_native_tool_calls_are_used(self):
        s = Script(call("write_file", path="a.txt", content="x"), reply("ok"))
        a = self.agent(s, use_plan=False)
        s.replies[0] = reply("", calls=[("write_file", {"path": "a.txt", "content": "x"})])
        a.run("do it")
        self.assertTrue((self.ws / "a.txt").exists())

    def test_nudge_when_no_tool_used(self):
        s = Script(reply(json.dumps({"steps": ["Write b.txt with the text yes"]})), reply("All done!"),
                   call("write_file", path="b.txt", content="yes"), reply("done"))
        self.agent(s).run("x")
        self.assertTrue((self.ws / "b.txt").exists())

    def test_verify_runs_after_change_and_fix_loop(self):
        (self.ws / "test_m.py").write_text("import unittest, m\nclass T(unittest.TestCase):\n"
                                           "    def test(self): self.assertEqual(m.f(), 2)\n")
        s = Script(reply(json.dumps({"steps": ["Write m.py with a function f that returns 2"]})),
                   call("write_file", path="m.py", content="def f():\n    return 1\n"), reply("wrote m.py"),
                   call("edit_file", path="m.py", old_string="return 1", new_string="return 2"), reply("fixed"))
        a = self.agent(s)
        a.cfg.verify = a.python_cmd("-m unittest -q")
        res = a.run("write m.py")
        self.assertEqual(res["status"], "passed")

    def test_unknown_and_repeated_calls(self):
        a = self.agent(Script())
        self.assertTrue(a.execute("nope", {})[1])
        for _ in range(3):
            a.execute("list_files", {"pattern": "*"})
        out, err = a.execute("list_files", {"pattern": "*"})
        self.assertTrue(err)
        self.assertIn("4 times", out)

    def test_hard_block(self):
        out, err = self.agent(Script(), mode="yolo").execute("run_command", {"command": "shutdown /s /t 0"})
        self.assertTrue(err)
        self.assertIn("hard-block", out)

    def test_outside_workspace_refused(self):
        out, err = self.agent(Script()).execute("write_file", {"path": "../evil.txt", "content": "x"})
        self.assertTrue(err)
        self.assertIn("outside the workspace", out)


class TestToolsetsAndConfirm(Base):
    def setUp(self):
        super().setUp()
        self.ran = []

        def extra_mod(agent):
            agent.registry.add_toolset("gadget", "a gadget", keywords=r"\bgadget\b")
            for lvl in ("never", "edit", "act", "always"):
                agent.registry.add(Tool(f"g_{lvl}", lvl, {}, [], lambda _l=lvl: (self.ran.append(_l) or "ok", False),
                                        "gadget", lvl))
        self.modules = [builtin, memory, extra_mod]

    def tool_names(self, payload):
        return {t["function"]["name"] for t in payload["tools"]}

    def test_inactive_toolset_hidden_until_used(self):
        s = Script(call("use_toolset", name="gadget"), reply("done"))
        a = self.agent(s, use_plan=False)
        a.run("hello")
        self.assertNotIn("g_never", self.tool_names(s.payloads[0]))
        self.assertIn("gadget", s.payloads[0]["messages"][0]["content"])   # listed as switchable
        self.assertIn("g_never", self.tool_names(s.payloads[1]))

    def test_keyword_routing(self):
        s = Script(reply("ok"))
        self.agent(s, use_plan=False).run("use the gadget please")
        self.assertIn("g_never", self.tool_names(s.payloads[0]))

    def test_confirm_levels(self):
        expect = {"auto": {"never": False, "edit": False, "act": True, "always": True},
                  "ask": {"never": False, "edit": True, "act": True, "always": True},
                  "yolo": {"never": False, "edit": False, "act": False, "always": True}}
        for mode, levels in expect.items():
            a = self.agent(Script(), mode=mode)
            for lvl, asks in levels.items():
                self.assertEqual(bool(a.needs_confirm(a.registry.tools[f"g_{lvl}"], {})), asks, (mode, lvl))

    def test_declined_action_does_not_run(self):
        a = self.agent(Script())
        self.answers = ["n"]
        out, err = a.execute("g_act", {})
        self.assertTrue(err)
        self.assertEqual(self.ran, [])
        self.answers = ["y"]
        self.assertFalse(a.execute("g_act", {})[1])
        self.assertEqual(self.ran, ["act"])

    def test_risky_command_guard(self):
        a = self.agent(Script())
        self.assertTrue(a.needs_confirm(a.registry.tools["run_command"], {"command": "git push origin main"}))
        self.assertFalse(a.needs_confirm(a.registry.tools["run_command"], {"command": "git status"}))


class TestMemory(Base):
    def test_remember_shows_in_prompt_and_forget(self):
        a = self.agent(Script())
        a.execute("remember", {"fact": "My notes live in Q:/zz-notes"})
        self.assertIn("Q:/zz-notes", a.system_prompt())
        a.execute("forget", {"text": "zz-notes"})
        self.assertNotIn("Q:/zz-notes", a.system_prompt())

    def test_secrets_are_redacted(self):
        a = self.agent(Script())
        a.execute("remember", {"fact": "key sk-or-v1-abcdefghijklmnopqrstuvwxyz123456"})
        self.assertNotIn("abcdefghij", a.system_prompt())
        a.execute("forget", {"text": "key"})


TOOL_CODE = textwrap.dedent('''
    def shout(text: str) -> str:
        return text.upper() + "!"

    TOOLS = [{"name": "shout", "description": "uppercase text", "function": shout,
              "parameters": {"text": {"type": "string"}}, "required": ["text"]}]
''')
TEST_CODE = textwrap.dedent('''
    import unittest
    from tool import shout

    class T(unittest.TestCase):
        def test(self):
            self.assertEqual(shout("hi"), "HI!")
''')


class TestSkills(Base):
    def setUp(self):
        super().setUp()
        self.repo = Path(tempfile.mkdtemp(prefix="omni-skillrepo-"))
        make_git_repo(self.repo)
        (self.repo / "README").write_text("x")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.modules = [builtin, memory, skills]

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.repo, ignore_errors=True)

    def mk(self):
        return self.agent(Script(), extra={"skills_dir": str(self.repo / "skills")})

    def test_create_approve_load_and_use(self):
        a = self.mk()
        self.answers = ["y"]
        out, err = a.execute("create_skill", {"name": "shouter", "description": "make text loud",
                                              "instructions": "Call shout.", "tool_code": TOOL_CODE,
                                              "test_code": TEST_CODE, "keywords": r"\bloud\b"})
        self.assertFalse(err, out)
        self.assertIn("committed", out)
        self.assertIn("skill: add shouter", git(self.repo, "log", "--format=%s").stdout)
        self.assertEqual(a.execute("shout", {"text": "hey"}), ("HEY!", False))
        b = self.mk()                                            # a fresh agent loads it from disk
        self.assertIn("shouter", b.skills_index())
        b.registry.reset_active()
        b.route_toolsets("make this loud")
        self.assertIn("shout", b.registry.active_names())

    def test_tool_code_without_test_is_rejected(self):
        a = self.mk()
        out, err = a.execute("create_skill", {"name": "nt", "description": "d", "instructions": "i",
                                              "tool_code": TOOL_CODE})
        self.assertTrue(err)
        self.assertIn("needs a unit test", out)
        self.assertFalse((self.repo / "skills" / "nt").exists())

    def test_failing_test_is_rejected(self):
        a = self.mk()
        out, err = a.execute("create_skill", {"name": "bad", "description": "d", "instructions": "i",
                                              "tool_code": TOOL_CODE, "test_code": TEST_CODE.replace("HI!", "NO")})
        self.assertTrue(err)
        self.assertIn("FAILED", out)

    def test_declined_skill_not_installed(self):
        a = self.mk()
        self.answers = ["n"]
        out, err = a.execute("create_skill", {"name": "plain", "description": "d", "instructions": "steps"})
        self.assertTrue(err)
        self.assertFalse((self.repo / "skills" / "plain").exists())

    def test_instructions_only_skill_and_delete(self):
        a = self.mk()
        self.answers = ["y", "y"]
        self.assertFalse(a.execute("create_skill", {"name": "plain", "description": "d", "instructions": "steps"})[1])
        out, _ = a.execute("load_skill", {"name": "plain"})
        self.assertIn("steps", out)
        self.assertIn("## Skill: plain", a.system_prompt())
        self.assertFalse(a.execute("delete_skill", {"name": "plain"})[1])
        self.assertFalse((self.repo / "skills" / "plain").exists())


FAKE_MCP = textwrap.dedent('''
    import json, sys
    print("a stray log line that is not JSON", flush=True)
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        meth = m["method"]
        if meth == "initialize":
            r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        elif meth == "tools/list":
            r = {"tools": [{"name": "add", "description": "add two numbers",
                            "inputSchema": {"type": "object", "properties": {"a": {"type": "number"},
                                            "b": {"type": "number"}}, "required": ["a", "b"]}}]}
        elif meth == "tools/call":
            a = m["params"]["arguments"]
            r = {"content": [{"type": "text", "text": str(a["a"] + a["b"])}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
''')


class TestMcp(Base):
    def setUp(self):
        super().setUp()
        self.server = self.ws / "fake_mcp.py"
        self.server.write_text(FAKE_MCP)
        self.cfg_path = self.ws / "mcp.json"
        self.modules = [builtin, memory, mcp]

    def test_add_server_call_tool_and_reload(self):
        a = self.agent(Script(), mcp_config=str(self.cfg_path))
        try:
            self.answers = ["y"]
            out, err = a.execute("add_mcp_server", {"name": "calc", "command": sys.executable,
                                                    "args": [str(self.server)], "description": "adds numbers"})
            self.assertFalse(err, out)
            self.assertEqual(a.execute("calc__add", {"a": 2, "b": 3}), ("5", False))
            self.assertIn("calc", json.loads(self.cfg_path.read_text())["mcpServers"])
        finally:
            a.close()
        b = self.agent(Script(), mcp_config=str(self.cfg_path))     # starts from the saved config
        try:
            self.assertIn("calc__add", b.registry.tools)
            self.assertNotIn("calc__add", b.registry.active_names())  # its toolset is off until needed
        finally:
            b.close()

    def test_bad_server_is_reported_not_saved(self):
        a = self.agent(Script(), mcp_config=str(self.cfg_path))
        self.answers = ["y"]
        out, err = a.execute("add_mcp_server", {"name": "broken", "command": "definitely-not-a-real-command-xyz"})
        self.assertTrue(err)
        self.assertFalse(self.cfg_path.exists())
        a.close()


class TestDoAsISay(Base):
    """Conversation memory, question vs task, requirement checks, asking the user, and learning from feedback."""

    def system_of(self, payload):
        return payload["messages"][0]["content"]

    def user_of(self, payload):
        return payload["messages"][-1]["content"]

    def test_question_is_answered_without_a_plan(self):
        s = Script(reply("Paris."), routes=[route("answer")])
        a = self.agent(s)
        res = a.run("what's the capital of France?")
        self.assertEqual(res, {"status": "answer", "text": "Paris."})
        self.assertFalse(any("steps" in (p.get("format") or {}).get("properties", {}) for p in s.payloads))
        self.assertEqual(a.turns[-1]["outcome"], "Paris.")

    def test_follow_up_sees_the_conversation(self):
        s = Script(reply(json.dumps({"steps": ["Write a.txt with the text one"]})),
                   call("write_file", path="a.txt", content="one"), reply("Wrote a.txt"),
                   reply(json.dumps({"steps": ["Write b.txt with the text one"]})),
                   call("write_file", path="b.txt", content="one"), reply("Wrote b.txt"),
                   routes=[route(task="Write a.txt containing one"),
                           route(task="Write b.txt containing one, like a.txt")])
        a = self.agent(s)
        a.run("write a.txt containing one")
        a.run("now the same for b.txt")
        second_route = [p for p in s.payloads if "kind" in (p.get("format") or {}).get("properties", {})][1]
        self.assertIn("Conversation so far", self.user_of(second_route))
        self.assertIn("write a.txt containing one", self.user_of(second_route))
        planner = [p for p in s.payloads if "steps" in (p.get("format") or {}).get("properties", {})][1]
        self.assertIn("like a.txt", self.user_of(planner))          # the rewritten, self-contained task
        self.assertTrue((self.ws / "b.txt").exists())

    def test_unmet_requirement_gets_fixed_and_rechecked(self):
        reqs = ["todo.md exists", "todo.md has exactly 3 tasks"]
        s = Script(reply(json.dumps({"steps": ["Write todo.md with 3 tasks as a checklist"]})),
                   call("write_file", path="todo.md", content="- [ ] a\n- [ ] b\n"), reply("Wrote todo.md"),
                   call("write_file", path="todo.md", content="- [ ] a\n- [ ] b\n- [ ] c\n"), reply("Added the 3rd task"),
                   routes=[route(task="Create todo.md with 3 tasks", requirements=reqs)],
                   reviews=[{"results": [{"met": True, "evidence": "written"}, {"met": False, "evidence": "only 2 tasks"}]},
                            {"results": [{"met": True, "evidence": "written"}, {"met": True, "evidence": "3 lines"}]}])
        res = self.agent(s).run("make todo.md with 3 tasks")
        self.assertEqual([c["met"] for c in res["requirements"]], [True, True])
        self.assertEqual((self.ws / "todo.md").read_text().count("- [ ]"), 3)
        fix_prompt = next(self.user_of(p) for p in s.payloads if "NOT met yet" in self.user_of(p))
        self.assertIn("exactly 3 tasks", fix_prompt)
        review = [p for p in s.payloads if "results" in (p.get("format") or {}).get("properties", {})][0]
        self.assertIn("write_file", self.user_of(review))           # evidence = real tool calls

    def test_requirements_appear_in_step_prompt(self):
        s = Script(reply(json.dumps({"steps": ["Write x.txt"]})), call("write_file", path="x.txt", content="x"),
                   reply("ok"), routes=[route(task="Write x.txt", requirements=["x.txt contains x"])],
                   reviews=[{"results": [{"met": True, "evidence": "ok"}]}])
        self.agent(s).run("write x.txt with x")
        step = next(p for p in s.payloads if "Now do ONLY step 1" in self.user_of(p))
        self.assertIn("x.txt contains x", self.user_of(step))

    def test_missing_info_is_asked_first(self):
        s = Script(reply(json.dumps({"steps": ["Write report.md"]})), call("write_file", path="report.md", content="r"),
                   reply("ok"), routes=[route(question="Which month should the report cover?"),
                                        route(task="Write report.md for March")])
        self.answers = ["March"]
        self.agent(s).run("write the monthly report")
        second = [p for p in s.payloads if "kind" in (p.get("format") or {}).get("properties", {})][1]
        self.assertIn("The user answered: March", self.user_of(second))

    def test_ask_user_tool(self):
        a = self.agent(Script())
        self.answers = ["the blue one"]
        self.assertEqual(a.execute("ask_user", {"question": "which?"}), ("The user answered: the blue one", False))
        self.answers = [""]
        out, err = a.execute("ask_user", {"question": "again?"})        # an empty answer
        self.assertIn("best judgment", out)

    def test_feedback_becomes_a_preference_used_later(self):
        s = Script(reply(json.dumps({"steps": ["Write r.md"]})), call("write_file", path="r.md", content="r"),
                   reply("ok"), reply(json.dumps({"steps": ["Write s.md"]})),
                   rules=["When writing reports, put them in reports/ as Markdown"])
        a = self.agent(s)
        a.run("write a report")
        from omni.commands import run_command
        out = run_command(a, "/feedback put reports in the reports folder")
        self.assertIn("reports/ as Markdown", out)
        a.run("write another report")
        planner = [p for p in s.payloads if "steps" in (p.get("format") or {}).get("properties", {})][-1]
        self.assertIn("reports/ as Markdown", self.user_of(planner))
        self.assertIn("reports/ as Markdown", self.system_of(s.payloads[-1]))
        a.execute("forget", {"text": "reports/"})

    def test_redo_reruns_last_task_with_feedback(self):
        s = Script(reply(json.dumps({"steps": ["Write r.md"]})), call("write_file", path="r.md", content="r"), reply("ok"),
                   rules=["When writing r.md, use a title"])
        a = self.agent(s)
        a.run("write r.md")
        from omni.commands import run_command
        run_command(a, "/redo add a title")
        last_route = [p for p in s.payloads if "kind" in (p.get("format") or {}).get("properties", {})][-1]
        self.assertIn("My feedback: add a title", self.user_of(last_route))
        a.execute("forget", {"text": "r.md"})

    def test_utf16_files_are_readable(self):
        (self.ws / "u16.txt").write_bytes("2 3 5 7".encode("utf-16"))
        out, err = self.agent(Script()).execute("read_file", {"path": "u16.txt"})
        self.assertFalse(err)
        self.assertIn("2 3 5 7", out)


class TestAlwaysAllow(Base):
    def test_always_allow_stops_asking_for_that_tool(self):
        a = self.agent(Script(), mode="ask")
        self.answers = ["a"]
        self.assertFalse(a.execute("write_file", {"path": "1.txt", "content": "1"})[1])
        self.assertFalse(a.execute("write_file", {"path": "2.txt", "content": "2"})[1])   # no answer needed now
        self.assertEqual(a.session_allow, {"write_file"})
        self.assertTrue(a.execute("edit_file", {"path": "1.txt", "old_string": "1", "new_string": "x"})[1])  # asks, "n"

    def test_risky_command_allowance_is_narrow(self):
        a = self.agent(Script(), mode="auto")
        tool = a.registry.tools["run_command"]
        self.assertEqual(a.allow_key(tool, {"command": "git push origin main"}), "run_command:git push")
        self.assertEqual(a.allow_key(tool, {"command": "git status"}), "run_command")

    def test_never_allowlisted(self):
        a = self.agent(Script(), modules=[builtin, memory, skills])
        self.assertIsNone(a.allow_key(Tool("x", "", {}, [], lambda: ("", False), "core", "always"), {}))
        from omni import browser
        b = self.agent(Script(), modules=[builtin, browser])
        b.browser.last_refs = {"1": '[1] button "Pay now"'}
        self.assertIsNone(b.allow_key(b.registry.tools["browser_click"], {"ref": 1}))


class TestSchedule(unittest.TestCase):
    def setUp(self):
        from omni import schedule
        self.s = schedule
        self.now = __import__("datetime").datetime(2026, 11, 10, 8, 0)        # a Tuesday

    def test_parse_and_next(self):
        dt = __import__("datetime")
        P, N = self.s.parse_when, self.s.next_run
        self.assertEqual(N(P("every 30m"), self.now), self.now + dt.timedelta(minutes=30))
        self.assertEqual(N(P("daily 09:00"), self.now), self.now.replace(hour=9))
        self.assertEqual(N(P("every day at 7:30"), self.now), dt.datetime(2026, 11, 11, 7, 30))
        self.assertEqual(N(P("weekdays 18:30"), dt.datetime(2026, 11, 14, 8, 0)), dt.datetime(2026, 11, 16, 18, 30))
        self.assertEqual(N(P("mon,fri 07:00"), self.now), dt.datetime(2026, 11, 13, 7, 0))
        self.assertEqual(N(P("in 20m", self.now), self.now), self.now + dt.timedelta(minutes=20))
        self.assertEqual(N(P("daily 9pm"), self.now), self.now.replace(hour=21))
        self.assertEqual(self.s.describe(P("weekdays 18:30")), "weekdays 18:30")
        for bad in ("whenever", "every 1m", "daily 25:00"):
            with self.assertRaises(ValueError):
                P(bad)

    def test_store_due_and_once(self):
        dt = __import__("datetime")
        st = self.s.Schedules(Path(tempfile.mkdtemp()) / "s.json")
        rep = st.add("check mail", "every 30m", "C:/w", now=self.now)
        one = st.add("say hi", "in 10m", "C:/w", now=self.now)
        later = self.now + dt.timedelta(minutes=31)
        self.assertEqual({it["id"] for it in st.due(later)}, {rep["id"], one["id"]})
        st.started(one["id"], later)
        st.started(rep["id"], later)
        items = {it["id"]: it for it in st.load()}
        self.assertFalse(items[one["id"]]["enabled"])                   # one-shot: done
        self.assertEqual(items[rep["id"]]["next_run"], (later + dt.timedelta(minutes=30)).isoformat(timespec="minutes"))
        self.assertEqual(st.due(later), [])
        self.assertTrue(st.remove(rep["id"]))


class TestScheduleTool(Base):
    def test_schedule_needs_approval(self):
        from omni import schedule
        a = self.agent(Script(), modules=[builtin, schedule])
        a.schedules = schedule.Schedules(self.ws / "s.json")
        self.answers = ["n"]
        self.assertTrue(a.execute("schedule_task", {"task": "x", "when": "daily 09:00"})[1])
        self.assertEqual(a.schedules.load(), [])


class TestDesktopHelpers(unittest.TestCase):
    def test_hotkeys(self):
        self.assertEqual(hotkey_to_keys("ctrl+s"), "^s")
        self.assertEqual(hotkey_to_keys("ctrl+shift+esc"), "^+{ESC}")
        self.assertEqual(hotkey_to_keys("alt+F4"), "%{F4}")
        self.assertEqual(hotkey_to_keys("win+r"), "{VK_LWIN down}r{VK_LWIN up}")
        self.assertEqual(hotkey_to_keys("enter"), "{ENTER}")
        with self.assertRaises(ValueError):
            hotkey_to_keys("hyper+x")

    def test_escape_text(self):
        self.assertEqual(escape_text("a+b (c)\nd"), "a{+}b {(}c{)}{ENTER}d")


class TestBrowserGuard(Base):
    def test_risky_click_asks_even_in_auto(self):
        from omni import browser
        a = self.agent(Script(), modules=[builtin, browser])
        a.browser.last_refs = {"1": '[1] button "Place order"', "2": '[2] link "Docs"'}
        self.assertTrue(a.needs_confirm(a.registry.tools["browser_click"], {"ref": 1}))
        self.assertFalse(a.needs_confirm(a.registry.tools["browser_click"], {"ref": 2}))

    def test_password_field_refused(self):
        from omni import browser
        a = self.agent(Script(), modules=[builtin, browser])

        class El:
            def get_attribute(self, k): return "password"
        a.browser.el = lambda ref: El()
        out, err = a.execute("browser_type", {"ref": 1, "text": "hunter2"})
        self.assertTrue(err)
        self.assertIn("password field", out)


class TestSelfUpdate(Base):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="omni-self-"))
        make_git_repo(self.root)
        (self.root / "omni").mkdir()
        (self.root / "omni" / "feature.py").write_text('"""A feature."""\nVALUE = 1\n')
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("")
        (self.root / "tests" / "test_feature.py").write_text(
            "import unittest\nfrom omni import feature\n"
            "class T(unittest.TestCase):\n    def test(self): self.assertIn(feature.VALUE, (1, 2))\n")
        (self.root / "omni" / "__init__.py").write_text("")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        self.modules = [builtin, selfupdate]

    def tearDown(self):
        super().tearDown()
        git(self.root, "worktree", "prune")
        shutil.rmtree(self.root, ignore_errors=True)

    def child_script(self, new_value):
        return Script(reply(json.dumps({"steps": ["Change VALUE in omni/feature.py to the new number"]})),
                      call("edit_file", path="omni/feature.py", old_string="VALUE = 1", new_string=f"VALUE = {new_value}"),
                      reply("changed VALUE"))

    def test_approved_change_is_merged_and_revertible(self):
        a = self.agent(self.child_script(2), extra={"self_root": str(self.root)})
        self.answers = ["y"]
        out, err = a.self_improve("set VALUE to 2")
        self.assertFalse(err, out)
        self.assertIn("VALUE = 2", (self.root / "omni" / "feature.py").read_text())
        self.assertTrue(a.restart_requested)
        self.assertIn("self-update: set VALUE to 2", git(self.root, "log", "--format=%s").stdout)
        self.assertNotIn("self/", git(self.root, "branch").stdout)       # temp branch cleaned up
        self.answers = ["y"]
        self.assertIn("Reverted", selfupdate.rollback(a, self.root))
        self.assertIn("VALUE = 1", (self.root / "omni" / "feature.py").read_text())

    def test_declined_change_leaves_repo_untouched(self):
        a = self.agent(self.child_script(2), extra={"self_root": str(self.root)})
        self.answers = ["n"]
        out, err = a.self_improve("set VALUE to 2")
        self.assertTrue(err)
        self.assertIn("VALUE = 1", (self.root / "omni" / "feature.py").read_text())
        self.assertEqual(git(self.root, "log", "--format=%s").stdout.strip(), "init")

    def test_change_that_breaks_tests_is_discarded(self):
        s = self.child_script(3)
        s.replies += [reply("could not fix")] * 10                      # fix attempts do nothing
        a = self.agent(s, extra={"self_root": str(self.root)})
        out, err = a.self_improve("set VALUE to 3")
        self.assertTrue(err)
        self.assertIn("FAILS", out)
        self.assertIn("VALUE = 1", (self.root / "omni" / "feature.py").read_text())

    def test_dirty_repo_refused(self):
        (self.root / "omni" / "feature.py").write_text("VALUE = 9\n")
        a = self.agent(Script(), extra={"self_root": str(self.root)})
        out, err = a.self_improve("anything")
        self.assertTrue(err)
        self.assertIn("uncommitted", out)


def tearDownModule():
    shutil.rmtree(_TMP_STATE, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
