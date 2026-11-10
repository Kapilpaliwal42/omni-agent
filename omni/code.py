"""Code navigation and symbol-level editing, built for a small model working in big files.

A 9B model can't hold a 40 KB file in context, and exact-string edits fail when it misremembers a line.
These tools let it work the way people do: search, look at the outline, read one function, replace one
function. Python files are handled with `ast` (so nested functions, methods and line ranges are exact);
other languages fall back to regex outlines.

Helpers here are also used by the harness: symbol-aware file context for prompts, the function around a
traceback line, and "which functions are relevant to this request" retrieval for self-update.
"""
from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

from .registry import Tool
from .util import clip

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".omni", ".cache", "dist", "build"}
TEXT_EXTS = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".html",
             ".css", ".sh", ".ps1", ".cfg", ".ini", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".cs", ".rb", ".php"}
REGEX_DEFS = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|func|fn|interface|type|struct|enum|impl)\s+([A-Za-z_]\w*)", re.M)


# ---------------------------------------------------------------- Python symbols

class Symbol:
    __slots__ = ("name", "qualname", "kind", "start", "end", "col", "parent", "doc")

    def __init__(self, name, qualname, kind, start, end, col, parent, doc):
        self.name, self.qualname, self.kind = name, qualname, kind
        self.start, self.end, self.col, self.parent, self.doc = start, end, col, parent, doc


def py_symbols(source: str) -> list[Symbol]:
    """Every function and class, nested ones included, with 1-based line ranges (decorators included)."""
    tree = ast.parse(source)
    out: list[Symbol] = []

    def walk(node, prefix: str, parent: Symbol | None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                kind = "class" if isinstance(child, ast.ClassDef) else "def"
                q = f"{prefix}{child.name}"
                doc = (ast.get_docstring(child) or "").strip().splitlines()
                sym = Symbol(child.name, q, kind, start, child.end_lineno, child.col_offset, parent, doc[0] if doc else "")
                out.append(sym)
                walk(child, q + ".", sym)
            else:
                walk(child, prefix, parent)
    walk(tree, "", None)
    return out


def find_symbol(symbols: list[Symbol], name: str) -> list[Symbol]:
    exact = [s for s in symbols if s.qualname == name]
    return exact or [s for s in symbols if s.name == name.split(".")[-1]
                     and (s.qualname.endswith("." + name) or s.name == name)]


def symbol_source(lines: list[str], s: Symbol, numbered: bool = True) -> str:
    chunk = lines[s.start - 1:s.end]
    if numbered:
        return "\n".join(f"{i:>5}| {l}" for i, l in enumerate(chunk, s.start))
    return "\n".join(chunk)


def symbol_at(path: Path, line: int) -> Symbol | None:
    """Innermost function/class containing a line (for tracebacks)."""
    try:
        syms = py_symbols(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return None
    inside = [s for s in syms if s.start <= line <= s.end]
    return min(inside, key=lambda s: s.end - s.start) if inside else None


def outline_text(path: Path, rel: str) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    n = text.count("\n") + 1
    if path.suffix == ".py":
        try:
            syms = py_symbols(text)
        except SyntaxError as e:
            return f"{rel} ({n} lines) has a SyntaxError at line {e.lineno}: {e.msg}"
        rows = [f"{'  ' * s.qualname.count('.')}{s.kind} {s.name}  [lines {s.start}-{s.end}]"
                + (f"  # {s.doc[:70]}" if s.doc else "") for s in syms]
    else:
        rows = []
        for m in REGEX_DEFS.finditer(text):
            rows.append(f"{m.group(0).strip()[:80]}  [line {text.count(chr(10), 0, m.start()) + 1}]")
    return f"{rel} ({n} lines)\n" + ("\n".join(rows) if rows else "(no functions or classes found)")


# ---------------------------------------------------------------- retrieval for prompts

def _words(s: str) -> set[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return {w for w in re.split(r"[^A-Za-z0-9]+", s.lower()) if len(w) >= 3}


STOP = {"the", "and", "for", "that", "this", "with", "from", "into", "add", "make", "new", "use", "should", "must",
        "file", "files", "like", "the", "its", "are", "not", "but", "when", "then", "each", "one", "two", "all",
        "def", "return", "self", "none", "true", "false", "str", "int", "dict", "list", "args", "agent"}


def relevant_symbols(files: list[tuple[Path, str]], query: str, k: int = 6, budget: int = 9000) -> str:
    """The functions most related to `query` (by name, docstring and body words), with their source and the
    lines in the enclosing function that use them (e.g. where a nested tool function is registered)."""
    q = _words(query) - STOP
    ids = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query))
    scored = []
    for path, rel in files:
        if path.suffix != ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            syms = py_symbols(text)
        except (SyntaxError, OSError):
            continue
        lines = text.splitlines()
        for s in syms:
            if s.end - s.start > 160:           # huge functions: show their outline instead
                continue
            body = "\n".join(lines[s.start - 1:s.end])
            name_w = _words(s.name)
            score = 4 * len(q & name_w) + 2 * len(q & _words(s.doc)) + min(len(q & _words(body)), 6) * 0.5
            if s.name in ids:
                score += 8
            if score > 0:
                scored.append((score, rel, s, lines))
    scored.sort(key=lambda x: -x[0])
    parts, used = [], 0
    for score, rel, s, lines in scored[:k * 2]:
        src = symbol_source(lines, s)
        uses = ""
        if s.parent:
            par = lines[s.parent.start - 1:s.parent.end]
            hits = [f"{i:>5}| {l}" for i, l in enumerate(par, s.parent.start)
                    if (s.name in l) and not (s.start <= i <= s.end)]
            if hits:
                uses = f"\n  where {s.parent.qualname}() uses it:\n" + "\n".join(hits[:6])
        block = f"--- {rel}: {s.kind} {s.qualname} ---\n{clip(src, 2500)}{uses}"
        if used + len(block) > budget:
            continue
        parts.append(block)
        used += len(block)
        if len(parts) >= k:
            break
    return "\n\n".join(parts)


TRACE_RE = re.compile(r'File "([^"]+)", line (\d+), in ([\w<>]+)')


def traceback_context(text: str, root: Path, budget: int = 6000) -> str:
    """Source of the project functions that appear in a traceback (innermost last), deduplicated."""
    seen, parts, used = set(), [], 0
    root = Path(root).resolve()
    for path_s, line, _fn in reversed(TRACE_RE.findall(text or "")):
        p = Path(path_s)
        try:
            p = p.resolve()
            rel = p.relative_to(root)
        except (ValueError, OSError):
            continue
        if SKIP_DIRS.intersection(rel.parts) or not p.is_file():
            continue
        s = symbol_at(p, int(line))
        key = (str(rel), s.qualname if s else line)
        if key in seen:
            continue
        seen.add(key)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if s:
            body = symbol_source(lines, s)
        else:
            a = max(int(line) - 8, 1)
            body = "\n".join(f"{i:>5}| {l}" for i, l in enumerate(lines[a - 1:a + 15], a))
        block = f"--- {rel}: {s.qualname if s else 'around line ' + line} (from the traceback) ---\n{clip(body, 2500)}"
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def similar_snippets(text: str, needle: str, n: int = 2) -> str:
    """Where in the file the model probably meant, when its old_string doesn't match exactly."""
    lines = text.splitlines()
    k = max(len(needle.splitlines()), 1)
    first = needle.strip().splitlines()[0].strip() if needle.strip() else ""
    cands = []
    for i in range(0, max(len(lines) - k + 1, 1)):
        window = "\n".join(lines[i:i + k])
        r = difflib.SequenceMatcher(None, window.strip(), needle.strip()).quick_ratio()
        if first and first in lines[i]:
            r += 0.5
        cands.append((r, i))
    cands.sort(reverse=True)
    out = []
    for r, i in cands[:n]:
        if r < 0.4:
            break
        out.append("\n".join(f"{j:>5}| {l}" for j, l in enumerate(lines[i:i + k + 1], i + 1)))
    return "\n...\n".join(out)


# ---------------------------------------------------------------- tools

def register(agent):
    reg = agent.registry
    reg.add_toolset("code", "code navigation: search_code, outline, read_symbol, replace_symbol, add_code",
                    default_on=bool(agent.cfg.extra.get("code_tools_on")),
                    keywords=r"\b(code|function|class|method|bug|fix|refactor|implement|test|tests|script|module|"
                             r"compile|error|traceback|exception|\w+\.(py|js|ts|go|rs|java|cs))\b")

    def files(glob: str = "**/*"):
        for p in agent.workspace.glob(glob):
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(agent.workspace).parts):
                yield p

    def search_code(pattern: str, glob: str = "**/*", context: int = 0):
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits, nfiles = [], 0
        for p in files(glob):
            if p.suffix not in TEXT_EXTS or p.stat().st_size > 2_000_000:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            found = False
            for i, l in enumerate(lines, 1):
                if rx.search(l):
                    found = True
                    rel = p.relative_to(agent.workspace).as_posix()
                    if context:
                        a, b = max(i - context, 1), min(i + context, len(lines))
                        hits.append(f"{rel}:{i}:\n" + "\n".join(f"{j:>5}| {lines[j - 1]}" for j in range(a, b + 1)))
                    else:
                        hits.append(f"{rel}:{i}: {l.strip()[:200]}")
                    if len(hits) >= 80:
                        break
            nfiles += found
            if len(hits) >= 80:
                hits.append("... (more matches; narrow the pattern or glob)")
                break
        return ("\n".join(hits) or f"No matches for {pattern!r}."), False

    def outline(path: str):
        f = agent.resolve(path)
        return outline_text(f, path), False

    def read_symbol(path: str, name: str):
        f = agent.resolve(path)
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix != ".py":
            return "read_symbol works on Python files; use search_code then read_file with an offset.", True
        syms = py_symbols(text)
        found = find_symbol(syms, name)
        if not found:
            close = difflib.get_close_matches(name.split(".")[-1], [s.name for s in syms], n=5)
            return f"No function or class {name!r} in {path}. Similar: {', '.join(close) or 'none'}. Try outline.", True
        lines = text.splitlines()
        return "\n\n".join(f"{s.kind} {s.qualname} (lines {s.start}-{s.end}):\n{symbol_source(lines, s)}"
                           for s in found[:3]), False

    def replace_symbol(path: str, name: str, new_code: str):
        f = agent.resolve(path)
        if f.suffix != ".py":
            return "replace_symbol works on Python files. Use edit_file for other files.", True
        text = f.read_text(encoding="utf-8")
        syms = py_symbols(text)
        found = find_symbol(syms, name)
        if len(found) != 1:
            return (f"{'No' if not found else len(found)} symbol(s) named {name!r} in {path}. Use the qualified "
                    f"name from outline (e.g. register.web_fetch).", True)
        s = found[0]
        lines = text.splitlines()
        new = _reindent(new_code, " " * s.col)
        if not re.match(r"\s*(@|def |async def |class )", new):
            return "new_code must be the complete function or class, starting with def/class (or a decorator).", True
        out = "\n".join(lines[:s.start - 1] + new.splitlines() + lines[s.end:]) + ("\n" if text.endswith("\n") else "")
        try:
            ast.parse(out)
        except SyntaxError as e:
            return f"Not saved: the result would have a SyntaxError at line {e.lineno}: {e.msg}. Fix new_code.", True
        f.write_text(out, encoding="utf-8")
        agent.changed_files.add(path)
        return f"Replaced {s.kind} {s.qualname} in {path} (was lines {s.start}-{s.end}, now {len(new.splitlines())} lines).", False

    def add_code(path: str, code: str, after: str = ""):
        f = agent.resolve(path)
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        lines = text.splitlines()
        if after:
            if f.suffix != ".py":
                return "after= works for Python files only; use edit_file elsewhere.", True
            found = find_symbol(py_symbols(text), after)
            if len(found) != 1:
                return f"Can't find exactly one symbol named {after!r} in {path}. Use outline.", True
            s = found[0]
            block = _reindent(code, " " * s.col).splitlines()
            out_lines = lines[:s.end] + [""] + block + lines[s.end:]
        else:
            out_lines = lines + ([""] if lines and lines[-1].strip() else []) + code.rstrip("\n").splitlines()
        out = "\n".join(out_lines) + "\n"
        if f.suffix == ".py":
            try:
                ast.parse(out)
            except SyntaxError as e:
                return f"Not saved: SyntaxError at line {e.lineno}: {e.msg}.", True
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(out, encoding="utf-8")
        agent.changed_files.add(path)
        return f"Added {len(code.splitlines())} lines to {path}" + (f" after {after}." if after else " at the end."), False

    guard = getattr(agent, "edit_guard", None)

    def guarded(fn):
        def run(**kw):
            if guard and (msg := guard(kw.get("path", ""))):
                return msg, True
            return fn(**kw)
        return run

    T = lambda *a, **k: reg.add(Tool(*a, toolset="code", **k))
    T("search_code", "Regex search across the workspace's text files. Returns file:line: text. context=N adds N "
      "lines around each hit. glob limits files, e.g. 'omni/**/*.py'.",
      {"pattern": {"type": "string"}, "glob": {"type": "string"}, "context": {"type": "integer"}}, ["pattern"], search_code)
    T("outline", "List the classes and functions in a file (nested ones too) with their line ranges.",
      {"path": {"type": "string"}}, ["path"], outline)
    T("read_symbol", "Show the source of one Python function, method or class by name (e.g. 'Agent.run' or "
      "'register.web_fetch'), with line numbers.", {"path": {"type": "string"}, "name": {"type": "string"}},
      ["path", "name"], read_symbol)
    T("replace_symbol", "Replace a whole Python function/method/class with new_code (the complete new definition). "
      "Safer than edit_file for big changes: indentation is fixed for you and syntax is checked before saving.",
      {"path": {"type": "string"}, "name": {"type": "string"}, "new_code": {"type": "string"}},
      ["path", "name", "new_code"], guarded(replace_symbol), confirm="edit")
    T("add_code", "Add new code to a file: after the function/class named in `after` (same indentation), or at "
      "the end. Syntax is checked before saving.",
      {"path": {"type": "string"}, "code": {"type": "string"}, "after": {"type": "string"}},
      ["path", "code"], guarded(add_code), confirm="edit")


def _reindent(code: str, indent: str) -> str:
    import textwrap
    body = textwrap.dedent(code.replace("\t", "    ")).strip("\n")
    return "\n".join((indent + l) if l.strip() else "" for l in body.splitlines())
