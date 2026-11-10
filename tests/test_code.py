"""Code navigation tools, safer edits, the own-source guard, and symbol-aware prompt context."""
from __future__ import annotations

import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

os.environ.setdefault("OMNI_STATE", tempfile.mkdtemp(prefix="omni-state-"))

from omni import builtin, code                                  # noqa: E402
from omni.code import relevant_symbols, traceback_context        # noqa: E402
from omni.core import Agent, Config                              # noqa: E402
from omni.util import ROOT                                       # noqa: E402

MOD = textwrap.dedent('''\
    """A module."""
    import os


    def register(agent):
        """Set things up."""
        def word_tool(path: str):
            return "words", False

        agent.tools.append(("word_tool", word_tool))


    class Box:
        def size(self):
            return 1

        def area(self):
            return self.size() * 2
    ''')


class CodeBase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="omni-code-"))
        (self.ws / "pkg").mkdir()
        (self.ws / "pkg" / "mod.py").write_text(MOD)
        self.a = Agent(Config(workspace=str(self.ws), rules="", mode="yolo", extra={"code_tools_on": True}),
                       ui_input=lambda p: "n", ui_print=lambda *a: None,
                       chat_fn=lambda p: {"message": {"content": "ok"}, "done_reason": "stop"}, modules=[builtin, code])

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def run_tool(self, _tool, **kw):
        return self.a.execute(_tool, kw)


class TestNavigation(CodeBase):
    def test_search_code(self):
        out, err = self.run_tool("search_code", pattern=r"def \w+\(self\)")
        self.assertFalse(err)
        self.assertIn("pkg/mod.py:14:", out)
        self.assertIn("pkg/mod.py:17:", out)
        out, _ = self.run_tool("search_code", pattern="word_tool", context=1)
        self.assertIn("|", out)

    def test_outline_includes_nested_and_methods(self):
        out, err = self.run_tool("outline", path="pkg/mod.py")
        self.assertFalse(err)
        for s in ("def register", "def word_tool", "class Box", "def area"):
            self.assertIn(s, out)
        self.assertIn("# Set things up.", out)

    def test_read_symbol_qualified_and_suggestions(self):
        out, err = self.run_tool("read_symbol", path="pkg/mod.py", name="Box.area")
        self.assertFalse(err)
        self.assertIn("return self.size() * 2", out)
        out, err = self.run_tool("read_symbol", path="pkg/mod.py", name="register.word_tool")
        self.assertIn('return "words", False', out)
        out, err = self.run_tool("read_symbol", path="pkg/mod.py", name="aera")
        self.assertTrue(err)
        self.assertIn("area", out)


class TestSymbolEdits(CodeBase):
    def test_replace_symbol_fixes_indentation(self):
        new = "def area(self):\n    return self.size() * 3\n"            # written at column 0
        out, err = self.run_tool("replace_symbol", path="pkg/mod.py", name="Box.area", new_code=new)
        self.assertFalse(err, out)
        text = (self.ws / "pkg" / "mod.py").read_text()
        self.assertIn("    def area(self):\n        return self.size() * 3", text)
        compile(text, "mod.py", "exec")

    def test_replace_symbol_refuses_broken_code(self):
        out, err = self.run_tool("replace_symbol", path="pkg/mod.py", name="Box.size", new_code="def size(self:\n  return")
        self.assertTrue(err)
        self.assertIn("SyntaxError", out)
        self.assertEqual((self.ws / "pkg" / "mod.py").read_text(), MOD)

    def test_add_code_after_symbol(self):
        out, err = self.run_tool("add_code", path="pkg/mod.py", after="Box.area",
                                 code="def volume(self):\n    return self.area() * 2")
        self.assertFalse(err, out)
        ns = {}
        exec((self.ws / "pkg" / "mod.py").read_text(), ns)
        self.assertEqual(ns["Box"]().volume(), 4)


class TestSaferEdits(CodeBase):
    def test_loose_match_ignores_indentation(self):
        out, err = self.run_tool("edit_file", path="pkg/mod.py",
                                 old_string="def size(self):\n    return 1", new_string="def size(self):\n    return 5")
        self.assertFalse(err, out)
        self.assertIn("ignoring whitespace", out)
        ns = {}
        exec((self.ws / "pkg" / "mod.py").read_text(), ns)
        self.assertEqual(ns["Box"]().size(), 5)

    def test_not_found_shows_closest_text(self):
        out, err = self.run_tool("edit_file", path="pkg/mod.py", old_string="return self.sise() * 2",
                                 new_string="return 0")
        self.assertTrue(err)
        self.assertIn("closest text", out)
        self.assertIn("self.size() * 2", out)

    def test_edit_that_breaks_syntax_is_not_saved(self):
        out, err = self.run_tool("edit_file", path="pkg/mod.py", old_string="return 1", new_string="return (1")
        self.assertTrue(err)
        self.assertIn("NOT saved", out)
        self.assertEqual((self.ws / "pkg" / "mod.py").read_text(), MOD)

    def test_write_file_wont_wipe_most_of_a_file(self):
        big = self.ws / "big.py"
        big.write_text("x = 1\n" * 400)
        out, err = self.run_tool("write_file", path="big.py", content="x = 2\n")
        self.assertTrue(err)
        self.assertIn("allow_shrink", out)
        self.assertFalse(self.run_tool("write_file", path="big.py", content="x = 2\n", allow_shrink=True)[1])

    def test_placeholder_warning(self):
        out, err = self.run_tool("write_file", path="new.py", content="def f():\n    # TODO: implement later\n    return None\n")
        self.assertFalse(err)
        self.assertIn("WARNING", out)
        self.assertIn("TODO", out)


class TestOwnSourceGuard(unittest.TestCase):
    def make(self, **extra):
        return Agent(Config(workspace=str(ROOT), rules="", mode="yolo", extra=extra), ui_input=lambda p: "n",
                     ui_print=lambda *a: None, chat_fn=lambda p: {"message": {"content": "ok"}}, modules=[builtin, code])

    def test_file_tools_cannot_edit_the_running_agent(self):
        a = self.make()
        for name, args in [("write_file", {"path": "omni/evil.py", "content": "x = 1"}),
                           ("edit_file", {"path": "omni/util.py", "old_string": "ROOT", "new_string": "R00T"}),
                           ("add_code", {"path": "omni/util.py", "code": "x = 1"}),
                           ("replace_symbol", {"path": "omni/util.py", "name": "clip", "new_code": "def clip(): pass"})]:
            out, err = a.execute(name, args)
            self.assertTrue(err, name)
            self.assertIn("self_improve", out, name)
        self.assertFalse((ROOT / "omni" / "evil.py").exists())

    def test_skill_files_point_to_create_skill(self):
        a = self.make()
        out, err = a.execute("write_file", {"path": "omni/skills/web_scraping/SKILL.md", "content": "x"})
        self.assertTrue(err)
        self.assertIn('"name": "create_skill"', out)
        self.assertIn('"name": "web-scraping"', out)
        self.assertFalse((ROOT / "omni" / "skills").exists())

    def test_state_folder_and_self_update_child_are_allowed(self):
        a = self.make()
        self.assertIsNone(a.edit_guard(".omni/scratch.txt"))
        self.assertIsNone(self.make(self_edit_ok=True).edit_guard("omni/util.py"))


class TestContext(CodeBase):
    def test_large_file_context_shows_outline_and_named_function(self):
        big = MOD + "\n" + "\n".join(f"def filler_{i}():\n    return {i}\n" for i in range(700))
        (self.ws / "pkg" / "mod.py").write_text(big)
        ctx = self.a.file_context("change Box.area in pkg/mod.py so area doubles size")
        self.assertIn("large file", ctx)
        self.assertIn("class Box", ctx)
        self.assertIn("return self.size() * 2", ctx)
        self.assertNotIn("filler_650():\n", ctx.split("outline")[-1][-200:])

    def test_traceback_brings_in_the_failing_function(self):
        tb = (f'Traceback (most recent call last):\n  File "{self.ws / "pkg" / "mod.py"}", line 18, in area\n'
              "    return self.size() * 2\nTypeError: boom")
        ctx = traceback_context(tb, self.ws)
        self.assertIn("Box.area", ctx)
        self.assertIn("return self.size() * 2", ctx)

    def test_relevant_symbols_find_nested_tool_and_its_registration(self):
        out = relevant_symbols([(self.ws / "pkg" / "mod.py", "pkg/mod.py")], "add a tool like word_tool that counts lines")
        self.assertIn("register.word_tool", out)
        self.assertIn('agent.tools.append(("word_tool", word_tool))', out)


if __name__ == "__main__":
    unittest.main()
