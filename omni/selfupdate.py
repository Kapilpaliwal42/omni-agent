"""Self-update: the agent changes its own code, safely and with quality gates.

1. A git worktree of the agent's own repo is made on a new branch (self/<id>) under .omni/selfupdate/.
2. A child agent makes the change there, with code-navigation tools (search_code, outline, read_symbol,
   replace_symbol, add_code) and a context pack: the functions most related to the request, file outlines,
   and short recipes for the usual changes (a tool, a slash command, a config flag, a test).
3. Quality gates, each failure fed back to the child for a focused fix round (up to --self-fix-rounds):
     - every changed Python file compiles, and the agent's modules import
     - the agent's own test suite passes
     - a code change comes with a test in tests/ that FAILS on the old code (so it really checks the change)
     - no placeholders/stubs in the added code
     - a review of the diff against the request (does it fully do what was asked?)
4. If an attempt still fails its gates, a fresh attempt starts from a clean worktree with notes on what went
   wrong (--self-attempts, default 2).
5. The diff and the gate report are shown to the user. Only on approval is it committed and merged.
6. The agent restarts to load the new code. `/rollback` reverts the last self-update commit.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .registry import Tool
from .util import ROOT, STATE, clip

TEST_ARGS = "-m unittest discover -s tests -t . -q"
PLACEHOLDER_RE = re.compile(r"\b(TODO|FIXME|XXX)\b|placeholder|implement(ed)? (this )?later|not implemented|"
                            r"NotImplementedError|your code here|^\+\s*\.\.\.\s*$", re.I | re.M)


def git(*args: str, cwd: Path = ROOT) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def source_files(root: Path) -> list[tuple[Path, str]]:
    fs = [root / "agent.py", root / "app.py", *sorted((root / "omni").glob("*.py")), *sorted((root / "tests").glob("*.py"))]
    return [(f, f.relative_to(root).as_posix()) for f in fs if f.is_file()]


def code_map(root: Path = ROOT) -> str:
    """Per file: its purpose, and the names of its functions (nested ones included), compactly."""
    from .code import py_symbols
    out = []
    for f, rel in source_files(root):
        try:
            text = f.read_text(encoding="utf-8")
            doc = (ast.get_docstring(ast.parse(text)) or "").splitlines()
            names = [s.qualname for s in py_symbols(text) if s.qualname.count(".") <= 1]
        except SyntaxError:
            doc, names = [], []
        line = f"- {rel}: {doc[0] if doc else ''}"
        if names and not rel.startswith("tests/"):
            line += "\n    " + ", ".join(names)[:600]
        out.append(line)
    return "\n".join(out)


RECIPES = r"""## How to make the usual changes (follow these patterns)

A new tool, inside an existing module's register(agent) function (e.g. omni/builtin.py):
    def word_count(path: str):
        text = agent.resolve(path).read_text(encoding="utf-8", errors="replace")
        return f"words: {len(text.split())}", False          # (output_text, is_error)
    reg.add(Tool("word_count", "Count the words in a text file.", {"path": {"type": "string"}}, ["path"],
                 word_count, "core"))                         # toolset; optional confirm="edit"|"act"|"always"
  Insert it with edit_file next to a similar existing tool (old_string = an existing reg.add(...) line).

A new slash command, in omni/commands.py: add "/name": "help text" to the COMMANDS dict, and a branch
    if cmd == "/name":
        return "text to show"
  inside run_command(agent, text), before its final `return f"Unknown command ..."`.

A new setting: add a field with a default to the Config dataclass in omni/core.py, and a matching
ap.add_argument(...) in build_config() in agent.py that passes it into Config(...).

A test (new file tests/test_<topic>.py). Tests never need Ollama, a network, a browser or a desktop:
    import os, tempfile, unittest
    os.environ.setdefault("OMNI_STATE", tempfile.mkdtemp())
    from omni.core import Agent, Config

    def make_agent():
        ws = tempfile.mkdtemp()
        fake_model = lambda payload: {"message": {"content": "ok"}, "done_reason": "stop"}
        return Agent(Config(workspace=ws, rules="", mode="yolo"), chat_fn=fake_model), ws

    class TestWordCount(unittest.TestCase):
        def test_counts_words(self):
            a, ws = make_agent()
            open(os.path.join(ws, "w.txt"), "w").write("one two three")
            out, err = a.execute("word_count", {"path": "w.txt"})   # tools are called like this
            self.assertFalse(err)
            self.assertIn("3", out)
  Slash commands are tested with: from omni.commands import run_command; run_command(a, "/name").
  Plain functions are tested by importing them, e.g. from omni.util import clip.
"""

SELF_RULES = """# You are improving your own source code

The workspace is a separate copy (git worktree) of the agent's own code. The running agent is untouched.

How to work:
1. Find the code involved first: search_code, outline, read_symbol. Never guess what a file contains.
2. Add a unit test in tests/ that checks the requested behaviour. It must fail on the current code.
3. Make the change: edit_file for small edits, replace_symbol for a whole function, add_code for new
   functions. Keep the existing style. Change only what the request needs.
4. Run the tests: {test_cmd}
Write real, complete, working code. No stubs, no placeholders, no fake results: a function must really do
what its name says. Keep every existing test passing. Never weaken the hard-block list, the confirmation
levels or the password-field refusal unless the request explicitly asks for it.

{recipes}

## Files
{code_map}

## The code most related to this request
{relevant}
"""

REVIEW_SYSTEM = """You review a code change against the request it was made for. Reply with JSON:
{"complete": true or false, "problems": ["..."]}
complete=false when: part of the request is not implemented, a function is a stub or returns fake/hard-coded
results, the change breaks something it touches, or the test does not really test the requested behaviour.
List concrete problems (file, function, what is wrong). Do not complain about style."""


def register(agent):
    reg = agent.registry
    if not agent.cfg.enable_self_update:
        return
    reg.add_toolset("self", "change your own code (the user approves the diff)", default_on=True)
    root = Path(agent.cfg.extra.get("self_root") or ROOT)     # the agent's own repo (tests point it elsewhere)
    g = lambda *a, cwd=root: git(*a, cwd=cwd)
    TEST_CMD = agent.python_cmd(TEST_ARGS)

    def child_agent(wt: Path, request: str):
        from . import builtin, code
        from .code import relevant_symbols
        from .core import Agent
        rules = wt / ".self-rules.md"
        rules.write_text(SELF_RULES.format(test_cmd=TEST_CMD, recipes=RECIPES, code_map=clip(code_map(wt), 6000),
                                           relevant=relevant_symbols(source_files(wt), request) or "(none found)"),
                         encoding="utf-8")
        cfg = dataclasses.replace(
            agent.cfg, workspace=str(wt), rules=str(rules), mode="auto", verify=TEST_CMD, fix_attempts=1,
            allow_outside=False, enable_self_update=False, model=agent.cfg.coder_model or agent.cfg.model,
            think=agent.cfg.coder_think or agent.cfg.think,
            extra=agent.cfg.extra | {"self_edit_ok": True, "code_tools_on": True, "force_task": True})
        child = Agent(cfg, ui_input=agent.input, ui_print=lambda *a: agent.print("   |", *a),
                      chat_fn=agent._chat_fn, modules=[builtin, code])
        child.structured, child.confirm_fn = agent.structured, agent.confirm_fn
        child.emit = lambda kind, **d: agent.emit(kind, child=True, **d)
        return child, rules

    def changed(wt: Path) -> list[str]:
        g("add", "-A", "--", ".", ":!.self-rules.md", cwd=wt)
        _, names = g("diff", "--cached", "--name-only", cwd=wt)
        return [n for n in names.splitlines() if n.strip()]

    def tests_fail_on_old_code(wt: Path, tests: list[str], sid: str) -> tuple[bool, str]:
        """Copy the new/changed tests into a clean checkout of the old code: they must fail there."""
        pristine = STATE / "selfupdate" / f"{sid}-pristine"
        code, out = g("worktree", "add", "-q", "--detach", str(pristine), "HEAD")
        if code:
            return True, f"(could not make a clean checkout to check the test: {out})"
        try:
            for t in tests:
                (pristine / t).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(wt / t, pristine / t)
            mods = " ".join(t[:-3].replace("/", ".") for t in tests)
            out, failed = agent.run_shell(agent.python_cmd(f"-m unittest {mods} -q"), 600, cwd=pristine)
            return failed, out
        finally:
            g("worktree", "remove", "--force", str(pristine))

    def review(child, request: str, diff: str) -> list[str]:
        prompt = f"Request:\n{request}\n\nThe change (git diff):\n{clip(diff, 12000)}"
        try:
            data = child.chat([{"role": "user", "content": prompt}], tools=False, system=REVIEW_SYSTEM,
                              fmt={"type": "object", "properties": {"complete": {"type": "boolean"}, "problems": {
                                  "type": "array", "items": {"type": "string"}}}, "required": ["complete", "problems"]})
            r = json.loads(data.get("message", {}).get("content", "{}"))
        except Exception:
            return []
        if r.get("complete", True):
            return []
        return [f"Review: {p}" for p in r.get("problems", [])[:5] if str(p).strip()] or ["Review: the change is incomplete."]

    def gates(child, wt: Path, request: str, sid: str, do_review: bool) -> tuple[list[str], str, dict]:
        """Run every quality gate. Returns (problems, context for fixing them, report)."""
        files = changed(wt)
        report = {"files": files}
        if not files:
            return ["No files were changed. Make the requested change."], "", report
        py = [f for f in files if f.endswith(".py") and (wt / f).is_file()]
        problems, ctx = [], []
        for f in py:
            try:
                compile((wt / f).read_text(encoding="utf-8"), f, "exec")
            except SyntaxError as e:
                problems.append(f"{f} line {e.lineno}: SyntaxError: {e.msg}")
        mods = [m for m, f in (("agent", "agent.py"), ("app", "app.py")) if (wt / f).is_file()]
        mods += [f"omni.{p.stem}" for p in sorted((wt / "omni").glob("*.py")) if p.stem != "__init__"]
        out, failed = (agent.run_shell(agent.python_cmd(f'-c "import {", ".join(mods)}"'), 120, cwd=wt)
                       if mods else ("", False))
        report["imports"] = not failed
        if failed:
            problems.append("Importing the agent fails:\n" + clip(out, 1500))
            ctx.append(out)
        out, failed = agent.run_shell(TEST_CMD, 900, cwd=wt)
        report["tests"] = not failed
        if failed:
            problems.append("The test suite fails:\n" + clip(out, 2500))
            ctx.append(out)
        code_changed = [f for f in py if f.startswith("omni/") or f in ("agent.py", "app.py")]
        test_files = [f for f in py if f.startswith("tests/") and Path(f).name.startswith("test_")]
        report["test_added"] = bool(test_files)
        if code_changed and not test_files:
            problems.append("The code changed but no test in tests/ checks it. Add tests/test_<topic>.py with a "
                            "test of the requested behaviour (see the test recipe).")
        elif code_changed and not failed:
            fails_old, out_old = tests_fail_on_old_code(wt, test_files, sid)
            report["test_checks_change"] = fails_old
            if not fails_old:
                problems.append(f"Your tests ({', '.join(test_files)}) also pass on the OLD code, so they don't check "
                                "the change. Make them test the new behaviour itself.")
        _, diff = g("diff", "--cached", cwd=wt)
        added = "\n".join(l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        src_added = "\n".join(chunk for name, chunk in _per_file_added(diff) if not name.startswith("tests/"))
        if hits := sorted({m.group(0).strip("+ ").strip() for m in PLACEHOLDER_RE.finditer(src_added)}):
            problems.append(f"The new code has placeholders or stubs ({', '.join(hits[:5])}). Write the real code.")
        report["placeholders"] = not hits
        if not problems and do_review:
            problems += review(child, request, diff)
            report["reviewed"] = True
        return problems, "\n".join(ctx), report

    def attempt(request: str, notes: list[str], n: int) -> dict:
        sid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-a{n}"
        wt = STATE / "selfupdate" / sid
        wt.parent.mkdir(parents=True, exist_ok=True)
        code, out = g("worktree", "add", "-q", "-b", f"self/{sid}", str(wt), "HEAD")
        if code:
            return {"ok": False, "fatal": f"git worktree add failed: {out}"}
        res = {"ok": False, "sid": sid, "wt": wt, "branch": f"self/{sid}"}
        child, rules = child_agent(wt, request)
        agent.print(f"\n[self-update] attempt {n}: working in {wt} on branch self/{sid}")
        agent.emit("self_update", phase="started", branch=f"self/{sid}", attempt=n)
        earlier = ("\n\nAn earlier attempt failed. Avoid these problems:\n" + "\n".join(f"- {x}" for x in notes[-6:])
                   ) if notes else ""
        task = (f"Change to make in your own code: {request}\n\nWork in this order: find the code involved "
                "(search_code / outline / read_symbol), add a test in tests/ that checks the new behaviour, make the "
                f"change with real working code, run the tests.{earlier}")
        child.run(task)
        rounds, reviews = agent.cfg.extra.get("self_fix_rounds", 3), 0
        problems, report = [], {}
        for r in range(rounds + 1):
            if agent.cancelled:
                break
            problems, ctx, report = gates(child, wt, request, sid, do_review=reviews < 2)
            reviews += bool(report.get("reviewed"))
            agent.emit("self_update_gates", attempt=n, round=r, problems=problems, report=report)
            if not problems:
                break
            agent.print(f"[self-update] attempt {n}, check {r + 1}: {len(problems)} problem(s): "
                        + " | ".join(p.splitlines()[0][:120] for p in problems))
            if r == rounds:
                break
            files = report.get("files", [])
            msgs = [{"role": "user", "content":
                     f"Change being made: {request}\n\nThe change is not ready yet. Problems found by the checks:\n"
                     + "\n".join(f"- {p}" for p in problems)
                     + child.file_context(" ".join(files) + "\n" + ctx, limit=4)
                     + "\n\nFix these with the tools (read the code first), then run the tests. Reply with one line "
                       "when done."}]
            child.loop(msgs, 15, require_action=True)
        rules.unlink(missing_ok=True)
        res.update(ok=not problems, problems=problems, report=report)
        return res

    def discard(res):
        g("worktree", "remove", "--force", str(res["wt"]))
        g("branch", "-D", res["branch"])

    def self_improve(request: str) -> tuple[str, bool]:
        if not (root / ".git").exists():
            return f"{root} is not a git repository, so self-update can't make a safe, revertible change.", True
        code, dirty = g("status", "--porcelain", "--untracked-files=no")
        if dirty:
            return ("The agent's own repo has uncommitted changes:\n" + dirty[:800]
                    + "\nCommit or stash them first, so a self-update can be reviewed and reverted on its own."), True
        notes: list[str] = []
        res: dict = {}
        for n in range(1, int(agent.cfg.extra.get("self_attempts", 2)) + 1):
            try:
                res = attempt(request, notes, n)
            except Exception as e:
                res = {"ok": False, "fatal": f"{type(e).__name__}: {e}"} | ({"wt": res.get("wt"), "branch": res.get("branch")}
                                                                          if res.get("wt") else {})
            if res.get("fatal"):
                if res.get("wt"):
                    discard(res)
                return f"Self-update failed: {res['fatal']}", True
            if res["ok"] or agent.cancelled:
                break
            notes += [p.splitlines()[0][:300] for p in res["problems"]]
            discard(res)
        if not res.get("ok"):
            if res.get("wt") and Path(res["wt"]).exists():
                discard(res)
            return ("Self-update failed its checks and was discarded. Nothing changed. Last problems:\n"
                    + "\n".join(f"- {p[:600]}" for p in res.get("problems", notes)[:6])), True

        wt, branch, rep = res["wt"], res["branch"], res["report"]
        _, stat = g("diff", "--cached", "--stat", cwd=wt)
        _, diff = g("diff", "--cached", cwd=wt)
        patch = STATE / "selfupdate" / f"{res['sid']}.diff"
        patch.write_text(diff, encoding="utf-8")
        checks = ("imports OK | tests PASSED | "
                  + ("new test fails on the old code (it checks the change)" if rep.get("test_checks_change")
                     else "no code changed, so no new test needed")
                  + " | no placeholders | review: " + ("passed" if rep.get("reviewed") else "skipped"))
        agent.print(f"\n===== Proposed change to the agent itself =====\n{diff}\n\n{stat}\nChecks: {checks}\n"
                    f"(diff saved to {patch})")
        if not agent.confirm("Apply this change to the agent's own code?"):
            discard(res)
            return "User declined the self-update. Nothing was changed.", True
        code, out = g("commit", "-q", "-m", f"self-update: {request[:72]}", cwd=wt)
        if code:
            discard(res)
            return f"git commit failed: {out}", True
        code, out = g("merge", "--no-edit", "-q", branch)
        if code:
            g("merge", "--abort")
            g("worktree", "remove", "--force", str(wt))
            return f"Merge into the running repo failed ({out}). The change is kept on branch {branch}.", True
        discard(res)
        agent.restart_requested = True
        _, sha = g("rev-parse", "--short", "HEAD")
        return (f"Applied self-update {sha} ({stat.splitlines()[-1].strip() if stat else 'changes'}). Checks: {checks}. "
                "The agent restarts after this task to load the new code. Undo with /rollback."), False

    agent.self_improve = self_improve
    reg.add(Tool("self_improve",
                 "Change your own code: add a tool or command, fix a bug in yourself, change your rules. A copy is "
                 "edited, tested and reviewed, then the user approves the diff. This is the ONLY way to change your "
                 "own files. Describe the change in detail: what, where, and how it should behave.",
                 {"request": {"type": "string", "description": "exactly what to change and how it should behave"}},
                 ["request"], self_improve, "self"))


def _per_file_added(diff: str) -> list[tuple[str, str]]:
    out, name, buf = [], "", []
    for l in diff.splitlines():
        if l.startswith("+++ b/"):
            if name:
                out.append((name, "\n".join(buf)))
            name, buf = l[6:], []
        elif l.startswith("+") and not l.startswith("+++"):
            buf.append(l)
    if name:
        out.append((name, "\n".join(buf)))
    return out


def rollback(agent, root: Path = ROOT) -> str:
    git_ = lambda *a: git(*a, cwd=root)
    _, log = git_("log", "--format=%h %s", "-n", "50")
    hits = [l for l in log.splitlines() if l.split(" ", 1)[-1].startswith(("self-update:", "skill:"))]
    if not hits:
        return "No self-update or skill commits to roll back."
    sha, msg = hits[0].split(" ", 1)
    if not agent.confirm(f"Revert {sha} '{msg}'? (adds a revert commit; nothing is deleted)"):
        return "Rollback cancelled."
    code, out = git_("revert", "--no-edit", sha)
    if code:
        git_("revert", "--abort")
        return f"Revert failed: {out}"
    agent.restart_requested = True
    return f"Reverted {sha}. Restarting to load the previous code."


def history() -> str:
    _, log = git("log", "--format=%h %ad %s", "--date=short", "-n", "30")
    lines = [l for l in log.splitlines() if "self-update:" in l or "skill:" in l or "Revert" in l]
    return "\n".join(lines) or "No self-updates yet."
