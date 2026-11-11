"""Agent loop: plan, run each step with a fresh context, check, report. Forked from worker-agent and
generalized from coding to any task (web, browser, desktop), with toolsets that load on demand."""
from __future__ import annotations

import datetime as dt
import json
import platform
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .registry import Registry, Tool
from .util import RISKY, STATE, clip, command_signature, extract_calls, matches, redact

MAX_LOOP_STEPS = 40
MAX_STEP_LOOP = 15
MAX_FAILED_IN_ROW = 5
MAX_SAME_CALL = 3
CHANGE_TOOLS = {"write_file", "edit_file", "replace_symbol", "add_code"}


class OllamaError(RuntimeError):
    pass


class Cancelled(Exception):
    """The user pressed Stop."""


@dataclass
class Config:
    workspace: str = "."
    rules: str = ""
    mode: str = "auto"                       # ask | auto | yolo
    model: str = "huihui_ai/qwen3.5-abliterated:9b"
    host: str = "http://127.0.0.1:11434"
    num_ctx: int = 16384
    temperature: float = 0.2
    think: str | None = "off"
    shell: str | None = None
    allow_outside: bool = False
    use_plan: bool = True
    verify: str | None = None
    fix_attempts: int = 2
    web: bool = True
    browser_headless: bool = False
    vision_model: str | None = None
    searxng_url: str | None = None
    mcp_config: str | None = None
    enable_self_update: bool = True
    coder_model: str | None = None           # model for self-update coding (default: model)
    coder_think: str | None = None           # "on" / "off" thinking for that model (default: think)
    extra: dict = field(default_factory=dict)


PLANNER_SYSTEM = """You plan work for an assistant that has tools (files, shell, web search, a web browser,
Windows desktop control, and learned skills). You only write the plan; you do not do the work.
Reply with JSON: {"steps": ["...", "..."]}

Example task: "Make a Python script that counts words in a file, with tests."
Example reply:
{"steps": [
  "Write wordcount.py completely: a count_words(path) function and a command-line entry point that prints the count",
  "Write test_wordcount.py with unittest tests for a normal file, an empty file and a missing file",
  "Run python -m unittest -v and fix any failures"
]}

Example task: "Find the current price of a Raspberry Pi 5 8GB on two shops and save it to prices.md"
Example reply:
{"steps": [
  "Search the web for Raspberry Pi 5 8GB price and pick two shop pages",
  "Open each shop page and read the price shown there",
  "Write prices.md with each shop, its price, the URL and today's date"
]}"""

PLAN_SCHEMA = {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
               "required": ["steps"]}

ROUTER_SYSTEM = """You read the user's latest message to an assistant that can use files, a shell, the web, a
browser and Windows apps. You do not do the work. Reply with JSON:
{"kind": "answer" or "task", "task": "...", "requirements": ["..."], "question": ""}
- kind "answer": a question, greeting or chat that needs no files changed and no actions on the computer.
- kind "task": the user wants something done (create or change files, run things, use a site or an app...).
- task: the request as one complete instruction (fill in details from the conversation if it refers back).
- requirements: for a task, EVERY concrete thing the user asked for, one short checkable sentence each, using the
  user's own names, formats, numbers and places. Nothing they didn't ask for. For an answer: [].
- question: only if the task truly cannot be done without information from the user. Otherwise "".

Example message: "make a file todo.md with 3 tasks for tomorrow, as a checklist"
Example reply:
{"kind": "task", "task": "Create todo.md containing 3 tasks for tomorrow written as a Markdown checklist",
 "requirements": ["A file named todo.md exists", "todo.md contains exactly 3 tasks for tomorrow",
                  "The tasks are written as a Markdown checklist (- [ ] ...)"], "question": ""}"""

ROUTE_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["answer", "task"]}, "task": {"type": "string"},
    "requirements": {"type": "array", "items": {"type": "string"}}, "question": {"type": "string"}},
    "required": ["kind", "task", "requirements", "question"]}

REVIEW_SYSTEM = """You check whether an assistant really did what the user asked. You only trust evidence: tool
outputs and file contents shown to you. The assistant's own claims are not evidence. Reply with JSON:
{"results": [{"met": true or false, "evidence": "a few words"}, ...]}  one entry per requirement, in order."""

REVIEW_SCHEMA = {"type": "object", "properties": {"results": {"type": "array", "items": {
    "type": "object", "properties": {"met": {"type": "boolean"}, "evidence": {"type": "string"}},
    "required": ["met", "evidence"]}}}, "required": ["results"]}

_BUILD_WORDS = re.compile(r"\b(create|write|add|implement|build|make|generate|refactor|rename|update|change)\b", re.I)
_CHECK_WORDS = re.compile(r"\b(run|test|tests|verify|check|ensure|debug|fix|confirm|re-run|rerun)\b", re.I)


def confirm_text(name: str, reason: str, args: dict, max_chars: int = 3000) -> str:
    """Readable permission prompt: one argument per line, multi-line values (file contents) as blocks."""
    lines = [f"{name}  ({reason})"]
    for k, v in args.items():
        v = redact(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
        if "\n" in v or len(v) > 100:
            body = v if len(v) <= max_chars else v[:max_chars] + f"\n... [{len(v) - max_chars} more chars]"
            lines.append(f"{k}:\n" + "\n".join("    " + l for l in body.splitlines()))
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def is_check_step(step: str) -> bool:
    return bool(_CHECK_WORDS.search(step)) and not _BUILD_WORDS.search(step)


class Agent:
    def __init__(self, cfg: Config, ui_input: Callable[[str], str] = input, ui_print: Callable[..., None] = print,
                 chat_fn: Callable[..., dict] | None = None, modules: list | None = None):
        self.cfg = cfg
        self.workspace = Path(cfg.workspace).resolve()
        if not self.workspace.is_dir():
            raise SystemExit(f"workspace not found: {self.workspace}")
        self.mode = cfg.mode
        self.shell = cfg.shell or ("powershell" if platform.system() == "Windows" else "bash")
        self.think = {"on": True, "off": False}.get(cfg.think or "")
        self.input, self.print = ui_input, ui_print
        self._chat_fn = chat_fn               # tests inject a scripted model here
        # UI hooks: the app sets these. emit() gets structured events (plan, tool_call, result...);
        # confirm_fn, when set, answers confirmations directly instead of reading y/N from input().
        self.emit: Callable[..., None] = lambda kind, **data: None
        self.confirm_fn: Callable[[str, str | None], bool | str] | None = None   # may return "always"
        self.ask_fn: Callable[[str], str] | None = None
        self.session_allow: set[str] = set()     # "always allow" answers, for this session only
        self.turns: list[dict] = []              # the conversation: what you asked and what came of it
        self.last_task: dict | None = None
        self.tool_log: deque[str] = deque(maxlen=24)   # recent tool calls, the evidence for requirement checks
        self.cancelled = False
        self.structured = False   # True in the app: progress goes out as events, not as printed text
        self._call_seq = 0
        self.registry = Registry()
        self.history: list[str] = []
        self.messages: list[dict] = []
        self.closers: list[Callable[[], None]] = []
        self.restart_requested = False
        self._last_call, self._same_count = None, 0
        self._changes_this_loop: set = set()
        self.changed_files: set[str] = set()
        self.rules = Path(cfg.rules).read_text(encoding="utf-8") if cfg.rules and Path(cfg.rules).is_file() else ""

        STATE.mkdir(exist_ok=True)
        (STATE / "logs").mkdir(exist_ok=True)
        self.log_path = STATE / "logs" / f"{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"

        from . import browser, builtin, code, desktop, mcp, memory, schedule, selfupdate, skills
        for mod in modules if modules is not None else (builtin, code, memory, skills, mcp, browser, desktop,
                                                         schedule, selfupdate):
            (mod.register if hasattr(mod, "register") else mod)(self)   # a module or a register function

    # ---------------------------------------------------- helpers

    def cli_print(self, *a):
        """Progress text for the terminal. The app shows the same progress from emit() events instead."""
        if not self.structured:
            self.print(*a)

    def log(self, **event):
        def shorten(v):
            if isinstance(v, str):
                return v if len(v) <= 2000 else v[:2000] + f"... [{len(v) - 2000} chars cut]"
            if isinstance(v, dict):
                return {k: shorten(x) for k, x in v.items()}
            if isinstance(v, list):
                return [shorten(x) for x in v]
            return v
        event = shorten(event)
        event["ts"] = dt.datetime.now().isoformat(timespec="seconds")
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(redact(json.dumps(event, ensure_ascii=False, default=str)) + "\n")

    def resolve(self, p: str) -> Path:
        full = (self.workspace / p).resolve()
        if not self.cfg.allow_outside and not full.is_relative_to(self.workspace):
            raise PermissionError(f"{p} is outside the workspace (start with --allow-outside to permit)")
        return full

    def confirm(self, summary: str, allow_key: str | None = None) -> bool:
        """Ask the user. allow_key (when given) lets them answer "always allow this for the session"."""
        if allow_key and allow_key in self.session_allow:
            return True
        if self.confirm_fn:
            ans = self.confirm_fn(summary, allow_key)
        else:
            hint = "[y/N/a = always this session]" if allow_key else "[y/N]"
            try:
                raw = self.input(f"\n  ? {summary}\n    allow? {hint} ").strip().lower()
            except EOFError:
                return False
            ans = "always" if allow_key and raw in ("a", "always") else raw in ("y", "yes")
        if ans == "always" and allow_key:
            self.session_allow.add(allow_key)
            self.emit("allow_list", keys=sorted(self.session_allow))
            return True
        return bool(ans)

    def allow_key(self, tool: Tool, args: dict) -> str | None:
        """What an "always allow" answer covers, or None when this call must always ask."""
        if tool.confirm == "always":
            return None                                   # self-changes and installs: every time
        if tool.name == "run_command":
            cmd = command_signature(args.get("command", ""))
            if matches(RISKY, cmd):                       # e.g. "git push": allows only that kind of command
                return "run_command:" + " ".join(cmd.split()[:2])
            return "run_command"
        if tool.guard and tool.guard(args):
            return None                                   # Buy / Send / Delete clicks: every time
        return tool.name

    def ask_user(self, question: str) -> str:
        """Ask the user a free-text question and wait for the answer."""
        self.emit("ask_user_open", question=question)
        if self.ask_fn:
            ans = self.ask_fn(question)
        else:
            try:
                ans = self.input(f"\n  ? {question}\n  > ")
            except EOFError:
                ans = ""
        return (ans or "").strip()

    def needs_confirm(self, tool: Tool, args: dict) -> str | None:
        """Reason to ask the user before this call, or None."""
        if tool.confirm == "always":
            return "this changes the agent itself or installs something"
        if self.mode == "yolo":
            return None
        if self.mode == "ask" and tool.confirm in ("edit", "act"):
            return "ask mode"
        if tool.confirm == "act":
            return "controls programs on this computer"
        if tool.guard and (reason := tool.guard(args)):
            return reason
        return None

    def python_cmd(self, args: str) -> str:
        """A shell command running this Python interpreter, quoted correctly for the shell in use."""
        exe = f'"{sys.executable}"'
        return f"& {exe} {args}" if self.shell == "powershell" else f"{exe} {args}"

    def run_shell(self, command: str, timeout_sec: int = 120, cwd: Path | None = None) -> tuple[str, bool]:
        if self.shell == "powershell":   # PS 5.1 writes UTF-16 with `>` by default; make files and output UTF-8
            command = ("$PSDefaultParameterValues['Out-File:Encoding']='utf8'; "
                       "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; " + command)
        argv = (["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
                if self.shell == "powershell" else [self.shell, "-lc", command])
        try:
            r = subprocess.run(argv, cwd=cwd or self.workspace, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=min(max(int(timeout_sec), 1), 1800))
        except subprocess.TimeoutExpired:
            return f"Timed out after {timeout_sec}s.", True
        return f"exit_code: {r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}", r.returncode != 0

    # ---------------------------------------------------- tool dispatch

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        preview = json.dumps(args, ensure_ascii=False)
        self.cli_print(f"  > {name} {redact(preview[:200])}")
        self._call_seq += 1
        cid = self._call_seq
        self.emit("tool_call", id=cid, name=name, args=json.loads(redact(preview)) if preview else {})
        sig = (name, command_signature(args.get("command", "")) if name == "run_command" else preview)
        self._same_count = self._same_count + 1 if sig == self._last_call else 1
        self._last_call = sig
        tool = self.registry.tools.get(name)
        if tool and tool.toolset not in self.registry.active:
            self.registry.active.add(tool.toolset)          # known tool, toolset just wasn't on: switch it on
        if not tool:
            out, err = f"Tool {name} does not exist. Use one of the listed tools, or use_toolset to see more.", True
        elif self._same_count > MAX_SAME_CALL:
            out, err = (f"You have made this exact call {self._same_count} times in a row. Stop repeating it: "
                        "change approach, or finish and report what is blocking you."), True
        elif name in CHANGE_TOOLS and sig in self._changes_this_loop:
            out, err = ("You already made this exact change and it did not solve the problem. "
                        "Re-read the error, then try a different fix."), True
        elif (reason := self.needs_confirm(tool, args)) and not self.confirm(
                confirm_text(name, reason, args), self.allow_key(tool, args)):
            out, err = "User declined this action. Take a different approach or ask what to do instead.", True
        else:
            try:
                out, err = tool.fn(**args)
            except TypeError as e:
                out, err = f"Bad arguments for {name}: {e}", True
            except (HTTPError, URLError, TimeoutError) as e:
                out, err = f"Network error: {e}", True
            except Exception as e:  # every tool failure goes back to the model; the loop never crashes
                out, err = f"{type(e).__name__}: {e}", True
        if name in CHANGE_TOOLS and not err:
            self._changes_this_loop.add(sig)
            self.changed_files.add(str(args.get("path", "")))
        out = clip(redact(str(out)))
        self.tool_log.append(f"{name}({redact(preview[:160])}) -> {'ERROR ' if err else ''}{out[:500]}")
        self.log(tool=name, args=args, is_error=err, output_chars=len(out))
        self.emit("tool_result", id=cid, name=name, output=out, error=bool(err))
        return out, err

    # ---------------------------------------------------- prompts

    def system_prompt(self) -> str:
        parts = [self.rules,
                 f"\n\n## Environment\n- Workspace folder: {self.workspace} (relative paths are inside it)\n"
                 f"- OS: {platform.system()} {platform.release()}\n- Shell for run_command: {self.shell}\n"
                 f"- Today: {dt.date.today().isoformat()}\n"]
        for hook in getattr(self, "prompt_hooks", []):     # memory, skills index
            if text := hook():
                parts.append("\n\n" + text)
        tools = self.registry.active_tools()
        lines = "\n".join(f"- {t.name}({', '.join(t.params)}): {t.description}" for t in tools)
        parts.append(f"""

## Tool protocol
To use a tool, reply with ONLY one JSON object, nothing else. Example:
{{"name": "write_file", "arguments": {{"path": "app.py", "content": "print('hi')\\n"}}}}
Rules:
- One tool call per reply. Wait for the result before the next call. Never invent results.
- Read a file before editing it. Check your work by running it or looking at the result.
- When the current job is finished, reply in plain text (no JSON): one or two lines saying what you did.

Tools:
{lines}""")
        if off := [s for s in self.registry.inactive_toolsets() if not s.name.startswith("skill:")]:
            parts.append("\n\nMore toolsets (switch one on with use_toolset):\n"
                         + "\n".join(f"- {s.name}: {s.description}" for s in off))
        return "".join(parts)

    # ---------------------------------------------------- model

    def chat(self, messages: list[dict], tools: bool = True, fmt: dict | None = None,
             system: str | None = None, model: str | None = None, images: list[str] | None = None) -> dict:
        payload = {"model": model or self.cfg.model, "stream": False,
                   "messages": [{"role": "system", "content": system or self.system_prompt()}] + messages,
                   "options": {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx, "num_predict": 4096}}
        if tools:
            payload["tools"] = [t.schema() for t in self.registry.active_tools()]
        if fmt:
            payload["format"] = fmt
        if self.think is not None and not model:
            payload["think"] = self.think
        if self._chat_fn:
            return self._chat_fn(payload)
        req = Request(self.cfg.host.rstrip("/") + "/api/chat", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=900) as r:
                data = json.load(r)
        except HTTPError as e:
            raise OllamaError(f"Ollama HTTP {e.code}: {e.read(2048).decode(errors='replace')}") from e
        except (URLError, TimeoutError, OSError) as e:
            raise OllamaError(f"Cannot reach Ollama at {self.cfg.host}: {e}") from e
        if data.get("error"):
            raise OllamaError(data["error"])
        return data

    # ---------------------------------------------------- context budget
    # Big tool results (a web page snapshot is ~8 KB) pile up inside one step and fill num_ctx. Before every
    # model call, older tool results are shrunk to a short note; the newest ones stay whole.

    KEEP_FULL = 2            # newest tool results kept in full
    CHARS_PER_TOKEN = 3.2    # rough, a bit pessimistic for mixed code/HTML text

    def estimate_tokens(self, messages: list[dict]) -> int:
        chars = len(self.system_prompt()) + sum(len(str(m.get("content", ""))) + 20 for m in messages)
        return int(chars / self.CHARS_PER_TOKEN)

    def context_overflow(self, data: dict) -> bool:
        """A "length" stop because the INPUT filled the window (tiny reply), not because the reply got long."""
        if data.get("done_reason") != "length":
            return False
        used = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        return (data.get("eval_count") or 0) < 300 or used >= self.cfg.num_ctx - 64

    @staticmethod
    def _shrink(m: dict, keep: int) -> bool:
        text = str(m.get("content", ""))
        if len(text) <= keep + 120 or "[trimmed:" in text[:keep + 200]:
            return False
        head = text[:keep].rsplit("\n", 1)[0] if "\n" in text[:keep] else text[:keep]
        m["content"] = f"{head}\n[trimmed: this older {m.get('tool_name', 'tool')} output was {len(text)} chars; " \
                       "call the tool again if you need it]"
        return True

    def compact(self, messages: list[dict], aggressive: int = 0) -> int:
        """Shrink old tool results so the conversation fits num_ctx. Returns how many were trimmed."""
        budget = self.cfg.num_ctx - 4096 - 512           # leave room for the reply (num_predict) and slack
        tools = [m for m in messages if m.get("role") == "tool"]
        keep_full = max(self.KEEP_FULL - aggressive, 1)
        trimmed = 0
        for m in tools[:-keep_full]:                    # always: older results become a short note
            trimmed += self._shrink(m, 400)
        if aggressive or self.estimate_tokens(messages) > budget:
            for m in tools[:-1]:
                trimmed += self._shrink(m, 160)
            last = tools[-1] if tools else None
            if last and self.estimate_tokens(messages) > budget:     # even the newest result is too big
                trimmed += self._shrink(last, 3000 if aggressive else 6000)
            for m in [m for m in messages if m.get("role") in ("assistant", "user")][1:-2]:
                if self.estimate_tokens(messages) <= budget:
                    break
                trimmed += self._shrink(m, 600)
        if trimmed:
            self.log(compacted=trimmed, est_tokens=self.estimate_tokens(messages), aggressive=aggressive)
        return trimmed

    def loop(self, messages: list[dict], max_steps: int = MAX_LOOP_STEPS,
             require_action: bool = False) -> tuple[str, bool]:
        used_tools, nudged, failed_in_row, overflow_retries = 0, False, 0, 0
        self._changes_this_loop = set()
        for _ in range(max_steps):
            if self.cancelled:
                raise Cancelled()
            self.compact(messages)
            self.emit("thinking")
            data = self.chat(messages)
            if self.cancelled:
                raise Cancelled()
            msg = data.get("message", {})
            content = msg.get("content", "") or ""
            if self.context_overflow(data) and overflow_retries < 2:
                # The input filled the context window, leaving no room to answer. Trim harder and ask again,
                # instead of stopping with a cut-off reply.
                overflow_retries += 1
                self.log(context_full=data.get("prompt_eval_count"), num_ctx=self.cfg.num_ctx, retry=overflow_retries)
                self.compact(messages, aggressive=overflow_retries)
                continue
            all_names = set(self.registry.tools)
            native = []
            for c in msg.get("tool_calls") or []:
                f = c.get("function", {})
                a = f.get("arguments") or {}
                try:
                    native.append((f.get("name"), json.loads(a) if isinstance(a, str) else a))
                except ValueError:
                    continue
            calls = [c for c in native if c[0] in all_names] or extract_calls(content, all_names)

            entry = {"role": "assistant", "content": content}
            if msg.get("tool_calls"):
                entry["tool_calls"] = msg["tool_calls"]
            messages.append(entry)
            self.log(model=data.get("model"), done_reason=data.get("done_reason"),
                     eval_count=data.get("eval_count"), calls=[c[0] for c in calls])

            if not calls:
                if data.get("done_reason") == "length":
                    why = (f"the context window (num_ctx={self.cfg.num_ctx}) is full even after trimming old tool "
                           "results; start a new conversation or split the task" if self.context_overflow(data)
                           else "the reply reached the output limit (num_predict=4096); ask for a shorter answer "
                                "or have it write long content to a file in parts")
                    return content.strip() + f"\n[stopped: {why}]", False
                if require_action and used_tools == 0 and not nudged:
                    nudged = True
                    messages.append({"role": "user", "content":
                                     "You did not call any tool, so nothing was actually done. "
                                     "Do the step now: reply with a tool call JSON."})
                    continue
                return content.strip(), True
            name, args = calls[0]
            used_tools += 1
            out, err = self.execute(name, args)
            note = "" if len(calls) == 1 else "\n[Only your first tool call was run. Issue the next one now if still needed.]"
            messages.append({"role": "tool", "tool_name": name, "content": f"{'ERROR ' if err else ''}{out}{note}"})
            if err and name != "run_command":
                failed_in_row += 1
            elif not err and name in CHANGE_TOOLS | {"run_command"}:
                failed_in_row = 0
            if failed_in_row >= MAX_FAILED_IN_ROW:
                self.log(stuck=f"{failed_in_row} failed calls in a row")
                return f"(stuck: {failed_in_row} failed tool calls in a row)", False
        return "", False

    # ---------------------------------------------------- planning and verification

    def workspace_files(self) -> list[Path]:
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__", ".omni", ".cache"}
        out = []
        for p in sorted(self.workspace.rglob("*")):
            if len(out) >= 2000:
                break
            if p.is_file() and not skip.intersection(p.relative_to(self.workspace).parts):
                out.append(p)
        return out

    def file_context(self, text: str, limit: int = 3, max_chars: int = 4000) -> str:
        exts = {".py", ".js", ".ts", ".tsx", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".html", ".css",
                ".sh", ".ps1", ".cfg", ".ini", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".csv"}
        from .code import find_symbol, outline_text, py_symbols, symbol_source, traceback_context
        parts = []
        idents = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text))
        for p in self.workspace_files():
            rel = p.relative_to(self.workspace).as_posix()
            if p.suffix not in exts or (p.name not in text and rel not in text):
                continue
            size = p.stat().st_size
            if size <= max_chars * 3:
                body = clip(redact(p.read_text(encoding="utf-8", errors="replace")), max_chars)
                parts.append(f"--- {rel} ---\n{body}")
            elif p.suffix == ".py" and size < 400_000:
                # Too big to paste whole: its outline, plus the functions this text mentions by name.
                src = p.read_text(encoding="utf-8", errors="replace")
                block = [f"--- {rel} (large file: outline, then the functions mentioned) ---",
                         clip(outline_text(p, rel), 1800)]
                try:
                    syms, lines, shown = py_symbols(src), src.splitlines(), 0
                    for name in sorted(idents, key=len, reverse=True):
                        for s in find_symbol(syms, name)[:1]:
                            if s.end - s.start < 200 and shown < 3:
                                block.append(f"{s.kind} {s.qualname}:\n" + clip(redact(symbol_source(lines, s)), 2500))
                                shown += 1
                except SyntaxError:
                    pass
                parts.append("\n".join(block))
            if len(parts) >= limit:
                break
        if 'File "' in text and "line " in text:
            if tb := traceback_context(text, self.workspace):
                parts.append(tb)
        return ("\n\nCurrent contents of the files involved:\n" + "\n\n".join(parts)) if parts else ""

    def file_overview(self, limit: int = 60) -> str:
        files = [str(p.relative_to(self.workspace)) for p in self.workspace_files()]
        more = f"\n... and {len(files) - limit} more" if len(files) > limit else ""
        return ("\n".join(files[:limit]) + more) if files else "(empty folder)"

    def route_toolsets(self, task: str):
        for s in self.registry.toolsets.values():
            if s.keywords and re.search(s.keywords, task, re.I):
                self.registry.active.add(s.name)

    # ---------------------------------------------------- understanding the request

    def conversation_context(self, n: int = 6) -> str:
        if not self.turns:
            return ""
        lines = []
        for t in self.turns[-n:]:
            lines.append(f"User: {clip(t['user'], 500)}")
            lines.append(f"You: {clip(t['outcome'], 600)}")
        return "Conversation so far:\n" + "\n".join(lines)

    def preferences(self) -> str:
        return getattr(self, "preferences_text", lambda: "")()

    def understand(self, text: str) -> dict:
        """One model call: is this a question or a task, what exactly is asked, and is anything missing?"""
        ctx = self.conversation_context()
        prompt = (f"{ctx}\n\nThe user's new message:\n{text}\n\n"
                  "If the message refers to earlier work ('do the same', 'that file', 'again'), write `task` "
                  "as a complete instruction that names the files, sites and details from the conversation.")
        default = {"kind": "task", "task": text, "requirements": [], "question": ""}
        try:
            data = self.chat([{"role": "user", "content": prompt}], tools=False, fmt=ROUTE_SCHEMA, system=ROUTER_SYSTEM)
            r = json.loads(data.get("message", {}).get("content", "{}"))
        except (ValueError, AttributeError):
            return default
        if not isinstance(r, dict):
            return default
        kind = r.get("kind") if r.get("kind") in ("answer", "task") else "task"
        reqs = [" ".join(str(x).split()) for x in r.get("requirements") or [] if str(x).strip()][:10]
        task = " ".join(str(r.get("task") or "").split()) or text
        if len(task) < len(text) // 3:          # a "rewrite" that lost most of the message: keep the original
            task = text
        return {"kind": kind, "task": task, "requirements": reqs, "question": str(r.get("question") or "").strip()}

    def make_plan(self, task: str, requirements: list[str] | None = None) -> list[str]:
        ctx = self.conversation_context(3)
        skills = getattr(self, "skills_index", lambda: "")()
        reqs = ("\n\nThe user's requirements (every one must be met):\n" + "\n".join(f"- {r}" for r in requirements)
                ) if requirements else ""
        prefs = self.preferences()
        prompt = (f"{ctx}\n\nTask: {task}{reqs}\n\n{prefs}\n\nFiles in the workspace:\n{self.file_overview()}"
                  f"{self.file_context(task)}\n\n{skills}\n\n"
                  "Use the exact function and class names that already exist in the files. "
                  "Split the task into 1 to 6 steps. Each step is one sentence saying exactly what to do, with "
                  "file names, sites or app names. Write each new file completely in ONE step. Only include work "
                  "the task asks for: no git commits, no deploys, no extra features. If the task involves code, "
                  "the last step runs the code or its tests and fixes failures. If the task produces a file, a "
                  "step reads it back to check its contents.")
        try:
            data = self.chat([{"role": "user", "content": prompt}], tools=False, fmt=PLAN_SCHEMA, system=PLANNER_SYSTEM)
            steps = json.loads(data.get("message", {}).get("content", "{}")).get("steps", [])
            steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()][:8]
        except (ValueError, AttributeError):
            steps = []
        if not steps or any(s in self.registry.tools or len(s.split()) < 3 for s in steps):
            self.log(plan_rejected=steps)
            return [task]
        return steps

    def step_prompt(self, task: str, steps: list[str], notes: list[str], i: int,
                    requirements: list[str] | None = None) -> str:
        plan = "\n".join(f"{k}. {s}{'  (done)' if k < i else ''}" for k, s in enumerate(steps, 1))
        done = "\n".join(f"- Step {k}: {n}" for k, n in enumerate(notes, 1)) or "- nothing yet"
        reqs = ("\n\nThe user's exact requirements:\n" + "\n".join(f"- {r}" for r in requirements)) if requirements else ""
        return (f"Overall task: {task}{reqs}\n\nPlan:\n{plan}\n\nAlready done:\n{done}"
                f"{self.file_context(task + ' ' + steps[i - 1])}\n\n"
                f"Now do ONLY step {i}: {steps[i - 1]}\n"
                "Use the tools. Follow the user's wording exactly (names, formats, places). Write complete, working "
                "code if code is needed: no placeholders or TODOs. If you cannot continue without information only "
                "the user has, use ask_user. When this step is complete, reply in plain text with one line saying "
                "what you did, including any facts you found that later steps need.")

    def review_requirements(self, task: str, requirements: list[str], notes: list[str]) -> list[dict]:
        """Check each requirement against evidence (tool outputs, file contents), not against the model's claims."""
        names = " ".join(requirements + sorted(self.changed_files)) + " " + task
        evidence = "\n".join(f"- {e}" for e in list(self.tool_log)[-12:]) or "- (no tool calls)"
        prompt = (f"Task: {task}\n\nRequirements:\n" + "\n".join(f"{k}. {r}" for k, r in enumerate(requirements, 1))
                  + "\n\nWhat the assistant says it did:\n" + "\n".join(f"- {n}" for n in notes)
                  + f"\n\nTool calls and their real outputs (the evidence):\n{evidence}"
                  + self.file_context(names, limit=4, max_chars=2500)
                  + "\n\nFor each requirement decide if the EVIDENCE shows it is met. A claim the evidence doesn't "
                    "show is not met. Garbled or wrong file contents are not met. Give the evidence in a few words.")
        try:
            data = self.chat([{"role": "user", "content": prompt}], tools=False, fmt=REVIEW_SCHEMA, system=REVIEW_SYSTEM)
            res = json.loads(data.get("message", {}).get("content", "{}")).get("results", [])
        except (ValueError, AttributeError):
            res = []
        out = []
        for k, r in enumerate(requirements):
            got = res[k] if k < len(res) and isinstance(res[k], dict) else {}
            out.append({"requirement": r, "met": bool(got.get("met")), "evidence": str(got.get("evidence", ""))[:300]})
        return out

    # ---------------------------------------------------- entry points

    def answer(self, text: str) -> dict:
        """A question or chat: answer directly (tools allowed for looking things up), no plan."""
        ctx = self.conversation_context()
        msgs = [{"role": "user", "content": f"{ctx}\n\nThe user says: {text}\n\n"
                 "Answer directly and briefly. Use a tool only if you need facts you don't have."}]
        reply, _ = self.loop(msgs, 10)
        reply = reply or "[model returned an empty reply]"
        self.cli_print(reply)
        self.emit("assistant", text=reply, finished=True)
        self.turns.append({"user": text, "outcome": reply})
        return {"status": "answer", "text": reply}

    def run(self, task: str) -> dict:
        self.log(user=task)
        self.registry.reset_active()
        self.changed_files = set()
        self.tool_log.clear()
        if not self.cfg.use_plan:
            self.route_toolsets(task)
            self.messages.append({"role": "user", "content": task})
            text, finished = self.loop(self.messages)
            self.cli_print(text or "[model returned an empty reply]")
            self.emit("assistant", text=text or "[model returned an empty reply]", finished=finished)
            if not finished:
                self.cli_print("[stopped before finishing; say 'continue' to keep going]")
            self.turns.append({"user": task, "outcome": text or ""})
            return {"status": "none", "text": text}

        self.emit("understanding")
        u = self.understand(task)
        if u["question"]:
            ans = self.ask_user(u["question"])
            if ans:
                u = self.understand(f"{task}\n(Asked: {u['question']} The user answered: {ans})") | {"question": ""}
        if self.cfg.extra.get("force_task"):
            u["kind"] = "task"
        self.route_toolsets(task + " " + u["task"])
        if u["kind"] == "answer":
            return self.answer(task)
        work, reqs = u["task"], u["requirements"]
        self.emit("requirements", task=work, requirements=reqs)
        if reqs:
            self.cli_print("Requirements:\n" + "\n".join(f"  - {r}" for r in reqs))

        self.emit("planning")
        steps = self.make_plan(work, reqs)
        self.emit("plan", steps=steps)
        self.cli_print("Plan:\n" + "\n".join(f"  {k}. {s}" for k, s in enumerate(steps, 1)))
        notes: list[str] = []
        for i, step in enumerate(steps, 1):
            if i > 1 and self.changed_files and all(is_check_step(s) for s in steps[i - 1:]):
                cmd = self.cfg.verify or self.detect_verify()
                if cmd and not self.run_shell(cmd, 600)[1]:
                    self.cli_print(f"\n[check passes; skipping remaining {len(steps) - i + 1} test/fix step(s)]")
                    notes += ["(skipped: check already passes)"] * (len(steps) - i + 1)
                    for k in range(i, len(steps) + 1):
                        self.emit("step_done", index=k, note="skipped: check already passes", finished=True)
                    break
            self.cli_print(f"\n[step {i}/{len(steps)}] {step}")
            self.emit("step_start", index=i, step=step)
            msgs = [{"role": "user", "content": self.step_prompt(work, steps, notes, i, reqs)}]
            text, finished = self.loop(msgs, MAX_STEP_LOOP, require_action=True)
            note = " ".join(text.split())[:400] or "(no summary)"
            if not finished:
                note = "(did not finish) " + note
            notes.append(note)
            self.cli_print(f"  = {note}")
            self.emit("step_done", index=i, note=note, finished=finished)

        status, cmd, tail = self.verify_and_fix(work)

        checked: list[dict] = []
        if reqs:
            self.emit("reviewing")
            checked = self.review_requirements(work, reqs, notes)
            missing = [c for c in checked if not c["met"]]
            if missing and not self.cancelled:
                self.emit("requirements_fix", missing=[c["requirement"] for c in missing])
                self.cli_print("\n[requirements not met yet] " + "; ".join(c["requirement"] for c in missing))
                msgs = [{"role": "user", "content":
                         f"Task: {work}\n\nThese requirements are NOT met yet:\n"
                         + "\n".join(f"- {c['requirement']} (seen: {c['evidence'] or 'no evidence'})" for c in missing)
                         + self.file_context(" ".join(c["requirement"] for c in missing) + " "
                                             + " ".join(self.changed_files))
                         + "\n\nFix them with the tools, then check the result (read the file back, re-run, or look "
                           "again). Reply with one line when done."}]
                fix_text, _ = self.loop(msgs, MAX_STEP_LOOP, require_action=True)
                notes.append("(requirement fix) " + " ".join(fix_text.split())[:300])
                checked = self.review_requirements(work, reqs, notes)
            self.emit("requirements_checked", results=checked)

        check = {"passed": f"PASSED  `{cmd}`", "failed": f"FAILED  `{cmd}`",
                 "none": "not run (no files changed, or no test command; --verify sets one)"}[status]
        self.cli_print("\n==== Result ====")
        for k, (s, n) in enumerate(zip(steps, notes), 1):
            self.cli_print(f"{k}. {s}\n   agent says: {n}")
        if checked:
            self.cli_print("Requirements (checked against tool outputs):")
            for c in checked:
                self.cli_print(f"  [{'x' if c['met'] else ' '}] {c['requirement']}  ({c['evidence']})")
        self.cli_print(f"Tests (run by the harness): {check}")
        if status == "failed":
            self.cli_print("Last check output:\n" + tail)
        met = sum(c["met"] for c in checked)
        outcome = " / ".join(notes)[:500] + (f" | requirements met: {met}/{len(checked)}" if checked else "")
        self.history.append(f"- Task: {work[:200]} | tests: {status} | {outcome[:300]}")
        self.turns.append({"user": task, "outcome": outcome})
        self.last_task = {"task": task, "work": work, "requirements": reqs, "outcome": outcome}
        self.log(result=status, steps=steps, notes=notes, requirements=checked)
        self.emit("result", status=status, command=cmd, output=tail, steps=steps, notes=notes, requirements=checked)
        return {"status": status, "steps": steps, "notes": notes, "requirements": checked}

    def detect_verify(self) -> str | None:
        r = self.workspace
        pkg = r / "package.json"
        if pkg.is_file():
            try:
                test = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}).get("test", "")
            except ValueError:
                test = ""
            if test and "no test specified" not in test:
                return "npm test --silent"
        if (r / "Cargo.toml").is_file():
            return "cargo test"
        if (r / "go.mod").is_file():
            return "go test ./..."
        tests = [p for p in self.workspace_files() if p.suffix == ".py"
                 and (p.name.startswith("test_") or p.name.endswith("_test.py"))]
        if tests:
            cfg = " ".join((r / n).read_text(encoding="utf-8", errors="ignore")
                           for n in ("pytest.ini", "pyproject.toml", "setup.cfg") if (r / n).is_file())
            pytest = "pytest" in cfg or any("import pytest" in p.read_text(encoding="utf-8", errors="ignore")
                                            for p in tests[:20])
            if (r / "tests").is_dir() and not pytest:
                return self.python_cmd("-m unittest discover -s tests -t . -v")
            return "python -m pytest -q" if pytest else "python -m unittest discover -v"
        return None

    def verify_and_fix(self, task: str) -> tuple[str, str, str]:
        """Harness-run check, only when files changed. Returns (passed|failed|none, command, output tail)."""
        cmd = self.cfg.verify or (self.detect_verify() if self.changed_files else None)
        if not cmd:
            return "none", "", ""
        out = ""
        for attempt in range(self.cfg.fix_attempts + 1):
            self.cli_print(f"\n[check] {cmd}")
            out, err = self.run_shell(cmd, 600)
            out = redact(out)
            self.log(check=cmd, passed=not err, attempt=attempt)
            self.emit("check", command=cmd, passed=not err, attempt=attempt, output=out[-2000:])
            if not err:
                return "passed", cmd, out[-1500:]
            if attempt == self.cfg.fix_attempts:
                break
            self.cli_print(f"[check] failed; fix attempt {attempt + 1}/{self.cfg.fix_attempts}")
            msgs = [{"role": "user", "content":
                     f"Overall task: {task}\n\nThe check `{cmd}` fails:\n{clip(out, 3000)}"
                     f"{self.file_context(task + ' ' + out)}\n\n"
                     "Decide whether the bug is in the code or in the test, based on what the task asks for. "
                     "Fix the cause with one focused change, then run the check to confirm. "
                     "When it passes, reply with one line."}]
            self.loop(msgs, MAX_STEP_LOOP, require_action=True)
        return "failed", cmd, out[-1500:]

    def close(self):
        for c in reversed(self.closers):
            try:
                c()
            except Exception:
                pass
