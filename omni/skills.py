"""Skills the agent learns: skills/<name>/SKILL.md (+ optional tool.py and test_tool.py).

SKILL.md starts with a small header:
    ---
    name: csv-report
    description: Turn a CSV into a Markdown summary report
    keywords: \\bcsv\\b|spreadsheet        (optional regex: switches the skill on when a task matches)
    ---
    Instructions for the model...

tool.py (optional) adds tools:
    def summarize(path: str) -> str: ...
    TOOLS = [{"name": "csv_summarize", "description": "...", "function": summarize,
              "parameters": {"path": {"type": "string"}}, "required": ["path"]}]

Every new or changed skill is written to a staging folder first, compiled, imported and unit-tested
there, then shown to the user in full. Only an approved skill is moved into skills/ and committed to git.
"""
from __future__ import annotations

import datetime as dt
import difflib
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .registry import Tool
from .util import ROOT, STATE, clip

SKILLS_DIR = ROOT / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,40}$")


def parse_skill_md(text: str) -> tuple[dict, str]:
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip():
                meta[k.strip().lower()] = v.strip()
        body = m.group(2)
    return meta, body.strip()


def git(*args: str, cwd: Path = ROOT) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def commit_paths(repo: Path, paths: list[str], message: str) -> str:
    """Commit just these paths (relative to repo), so every skill change can be reverted on its own."""
    if not (repo / ".git").exists():
        return "not a git repo, so not committed"
    git("add", "-A", "--", *paths, cwd=repo)
    code, out = git("commit", "-q", "-m", message, "--", *paths, cwd=repo)
    if code != 0 and "nothing to commit" not in out:
        return f"(git commit failed: {out[:200]})"
    _, sha = git("rev-parse", "--short", "HEAD", cwd=repo)
    return f"committed {sha}"


def check_skill_dir(d: Path) -> tuple[bool, str]:
    """Compile, import and test a staged skill in a separate process. Returns (ok, report)."""
    report = []
    tool = d / "tool.py"
    if tool.is_file():
        probe = ("import importlib.util,sys;s=importlib.util.spec_from_file_location('t',r'%s');m=importlib.util.module_from_spec(s);"
                 "s.loader.exec_module(m);T=getattr(m,'TOOLS',None);"
                 "assert isinstance(T,list) and T,'tool.py must define a non-empty TOOLS list';"
                 "[(callable(t['function']) and t['name'] and t['description']) or sys.exit('bad TOOLS entry: %%r'%%t) for t in T];"
                 "print('tools:',', '.join(t['name'] for t in T))") % tool
        r = subprocess.run([sys.executable, "-c", probe], cwd=d, capture_output=True, text=True, timeout=60)
        report.append(f"import check: {'ok' if r.returncode == 0 else 'FAILED'}\n{(r.stdout + r.stderr).strip()}")
        if r.returncode != 0:
            return False, "\n".join(report)
        if not list(d.glob("test_*.py")):
            report.append("tool.py has no test_tool.py: a skill with code needs a unit test.")
            return False, "\n".join(report)
    if list(d.glob("test_*.py")):
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"],
                           cwd=d, capture_output=True, text=True, timeout=180)
        report.append(f"unit tests: {'passed' if r.returncode == 0 else 'FAILED'}\n{clip((r.stdout + r.stderr).strip(), 3000)}")
        if r.returncode != 0:
            return False, "\n".join(report)
    return True, "\n".join(report) or "no code, nothing to test"


def register(agent):
    reg = agent.registry
    reg.add_toolset("skills", "list, load, create and delete learned skills", default_on=True)
    skills: dict[str, dict] = {}
    skills_dir = Path(agent.cfg.extra.get("skills_dir") or SKILLS_DIR)
    repo = skills_dir.parent

    def load_one(d: Path) -> str | None:
        md = d / "SKILL.md"
        if not md.is_file():
            return None
        meta, body = parse_skill_md(md.read_text(encoding="utf-8"))
        name = meta.get("name") or d.name
        ts = f"skill:{name}"
        reg.remove_toolset(ts)
        reg.add_toolset(ts, meta.get("description", ""), keywords=meta.get("keywords", ""))
        info = {"name": name, "description": meta.get("description", ""), "body": body, "dir": d, "tools": [], "error": ""}
        tool_py = d / "tool.py"
        if tool_py.is_file():
            try:
                spec = importlib.util.spec_from_file_location(f"omni_skill_{name.replace('-', '_')}", tool_py)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                for t in getattr(mod, "TOOLS", []):
                    f = t["function"]

                    def call(_f=f, **kw):
                        out = _f(**kw)
                        return out if isinstance(out, tuple) else (str(out), False)
                    reg.add(Tool(t["name"], t["description"], t.get("parameters", {}), t.get("required", []),
                                 call, ts, t.get("confirm", "never")))
                    info["tools"].append(t["name"])
            except Exception as e:
                info["error"] = f"{type(e).__name__}: {e}"
        skills[name] = info
        return name

    def load_all():
        skills.clear()
        if skills_dir.is_dir():
            for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
                load_one(d)

    def index() -> str:
        if not skills:
            return ""
        return ("## Skills you have learned (load_skill gives the full instructions)\n"
                + "\n".join(f"- {s['name']}: {s['description']}" + (" [broken]" if s["error"] else "")
                            for s in skills.values()))

    def prompt_hook() -> str:
        parts = [index()]
        for s in skills.values():
            if f"skill:{s['name']}" in reg.active and s["body"]:
                parts.append(f"## Skill: {s['name']}\n{clip(s['body'], 2500)}")
        return "\n\n".join(p for p in parts if p)

    def list_skills():
        return (index() or "No skills yet. Use create_skill to save one."), False

    def load_skill(name: str):
        if name not in skills:
            return f"No skill named {name}. " + (index() or "No skills yet."), True
        s = skills[name]
        reg.active.add(f"skill:{name}")
        tools = f"\nTools now available: {', '.join(s['tools'])}" if s["tools"] else ""
        err = f"\nWARNING: its tool code failed to load: {s['error']}" if s["error"] else ""
        return f"Skill {name}:\n{s['body']}{tools}{err}", bool(s["error"])

    def create_skill(name: str, description: str, instructions: str, tool_code: str = "",
                     test_code: str = "", keywords: str = ""):
        name = name.strip().lower()
        if not NAME_RE.match(name):
            return "Skill name must be lowercase letters, digits and dashes, 2-41 chars.", True
        if keywords:
            try:
                re.compile(keywords)
            except re.error as e:
                return f"keywords is not a valid regex: {e}", True
        stage = STATE / "staging" / "skills" / name
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        header = f"---\nname: {name}\ndescription: {' '.join(description.split())}\n"
        header += f"keywords: {keywords}\n" if keywords else ""
        (stage / "SKILL.md").write_text(header + "---\n" + instructions.strip() + "\n", encoding="utf-8")
        if tool_code.strip():
            (stage / "tool.py").write_text(tool_code, encoding="utf-8")
        if test_code.strip():
            (stage / "test_tool.py").write_text(test_code, encoding="utf-8")
        ok, report = check_skill_dir(stage)
        if not ok:
            return f"Skill {name} failed its checks and was NOT installed. Fix and call create_skill again.\n{report}", True

        target = skills_dir / name
        existed = target.exists()
        shown = []
        for f in sorted(stage.iterdir()):
            new = f.read_text(encoding="utf-8")
            old_f = target / f.name
            if old_f.is_file():
                diff = "".join(difflib.unified_diff(old_f.read_text(encoding="utf-8").splitlines(True),
                                                    new.splitlines(True), f"old/{f.name}", f"new/{f.name}"))
                shown.append(diff or f"(no change in {f.name})")
            else:
                shown.append(f"--- new file {f.name} ---\n{new}")
        agent.print(f"\n===== {'Update' if existed else 'New'} skill: {name} =====\n" + "\n".join(shown)
                    + f"\n----- checks -----\n{report}\n")
        if not agent.confirm(f"Install skill '{name}' into {target}?"):
            return "User declined installing this skill.", True
        if target.exists():
            trash = STATE / "trash" / "skills" / f"{name}-{dt.datetime.now():%Y%m%d-%H%M%S}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(trash))
        skills_dir.mkdir(exist_ok=True)
        shutil.copytree(stage, target, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(stage, ignore_errors=True)
        load_one(target)
        reg.active.add(f"skill:{name}")
        git_note = commit_paths(repo, [target.relative_to(repo).as_posix()], f"skill: {'update' if existed else 'add'} {name}")
        return f"Installed skill {name} ({git_note}). Tools: {', '.join(skills[name]['tools']) or 'none'}", False

    def delete_skill(name: str):
        if name not in skills:
            return f"No skill named {name}.", True
        if not agent.confirm(f"Delete skill '{name}'? (moved to .omni/trash, and the removal is committed)"):
            return "User declined.", True
        d = skills[name]["dir"]
        trash = STATE / "trash" / "skills" / f"{name}-{dt.datetime.now():%Y%m%d-%H%M%S}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(trash))
        reg.remove_toolset(f"skill:{name}")
        skills.pop(name)
        return f"Deleted skill {name} ({commit_paths(repo, [d.relative_to(repo).as_posix()], f'skill: remove {name}')}).", False

    load_all()
    agent.prompt_hooks = getattr(agent, "prompt_hooks", []) + [prompt_hook]
    agent.skills_index = index
    agent.skills = skills
    agent.reload_skills = load_all

    reg.add(Tool("list_skills", "List the skills you have learned.", {}, [], list_skills, "skills"))
    reg.add(Tool("load_skill", "Load a skill's full instructions and switch on its tools.",
                 {"name": {"type": "string"}}, ["name"], load_skill, "skills"))
    reg.add(Tool("create_skill",
                 "Save a reusable skill: instructions for a kind of task, plus optional Python tool code. "
                 "tool_code must define TOOLS = [{'name','description','function','parameters','required'}] and "
                 "then test_code (unittest, imports from tool) is required. It is tested, then the user approves it.",
                 {"name": {"type": "string", "description": "lowercase-with-dashes"},
                  "description": {"type": "string", "description": "one line: when to use it"},
                  "instructions": {"type": "string", "description": "step-by-step how-to for the model"},
                  "tool_code": {"type": "string"}, "test_code": {"type": "string"},
                  "keywords": {"type": "string", "description": "optional regex that switches it on for matching tasks"}},
                 ["name", "description", "instructions"], create_skill, "skills"))
    reg.add(Tool("delete_skill", "Delete a learned skill (asks the user).", {"name": {"type": "string"}},
                 ["name"], delete_skill, "skills"))
