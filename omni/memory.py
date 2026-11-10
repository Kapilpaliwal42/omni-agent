"""Long-term memory, kept between runs and shown in every prompt.

- .omni/memory.md       facts about you and your setup ("my notes are in D:/notes")
- .omni/preferences.md  how you want tasks done, learned from your feedback ("reports go in reports/ as Markdown")

Preferences come from /feedback (or the feedback box under a result in the app): the model turns your
correction into a short general rule, so it applies to future tasks of the same kind, not just this one.
"""
from __future__ import annotations

import datetime as dt
import json

from .registry import Tool
from .util import STATE, redact

MAX_PROMPT_LINES = 40

RULE_SYSTEM = """You turn a user's feedback on a finished task into ONE short, general rule for future tasks.
Keep the user's specifics (folders, formats, names, tools). Start with the situation, e.g. "When writing reports, ...".
Reply with JSON: {"rule": "..."}"""


def register(agent):
    path = STATE / "memory.md"
    prefs_path = STATE / "preferences.md"
    reg = agent.registry
    reg.add_toolset("memory", "remember facts and preferences about the user", default_on=True)

    def read_lines(p) -> list[str]:
        return [l for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- ")] if p.is_file() else []

    def lines() -> list[str]:
        return read_lines(path)

    def add_line(p, text: str) -> str:
        text = redact(" ".join(text.split()))[:300]
        if not text:
            return ""
        if any(text.lower() in l.lower() for l in read_lines(p)):
            return text
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {text} ({dt.date.today().isoformat()})\n")
        return text

    def preferences_text() -> str:
        ls = read_lines(prefs_path)[-MAX_PROMPT_LINES:]
        return ("## How the user wants things done (follow these)\n" + "\n".join(ls)) if ls else ""

    def prompt_hook() -> str:
        ls = lines()[-MAX_PROMPT_LINES:]
        mem = ("## What you remember (from earlier sessions)\n" + "\n".join(ls)) if ls else ""
        return "\n\n".join(x for x in (mem, preferences_text()) if x)

    def remember(fact: str):
        fact = add_line(path, fact)
        return (f"Remembered: {fact}", False) if fact else ("Nothing to remember.", True)

    def forget(text: str):
        removed = 0
        for p in (path, prefs_path):
            if p.is_file():
                ls = read_lines(p)
                keep = [l for l in ls if text.lower() not in l.lower()]
                removed += len(ls) - len(keep)
                p.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        return f"Forgot {removed} line(s).", removed == 0

    def save_preference(rule: str):
        rule = add_line(prefs_path, rule)
        return (f"Saved preference: {rule}", False) if rule else ("Empty preference.", True)

    def learn_feedback(feedback: str, task: str = "") -> str:
        """Turn feedback into a general rule (one model call) and save it. Returns the rule."""
        rule = ""
        try:
            data = agent.chat([{"role": "user", "content": f"The task was: {task or '(not given)'}\n"
                                f"The user's feedback: {feedback}"}], tools=False, system=RULE_SYSTEM,
                              fmt={"type": "object", "properties": {"rule": {"type": "string"}}, "required": ["rule"]})
            rule = str(json.loads(data.get("message", {}).get("content", "{}")).get("rule", "")).strip()
        except Exception:
            rule = ""
        if len(rule) < 8:                     # the model gave nothing useful: keep the user's own words
            rule = feedback if not task else f"When doing tasks like '{task[:80]}': {feedback}"
        return add_line(prefs_path, rule)

    agent.prompt_hooks = getattr(agent, "prompt_hooks", []) + [prompt_hook]
    agent.preferences_text = preferences_text
    agent.learn_feedback = learn_feedback
    agent.preference_lines = lambda: read_lines(prefs_path)
    reg.add(Tool("remember", "Save a short fact for future sessions (a path, an account name, a setup detail). "
                 "Never save passwords or keys.", {"fact": {"type": "string"}}, ["fact"], remember, "memory"))
    reg.add(Tool("save_preference", "Save how the user wants a kind of task done, when they tell you or correct "
                 "you (e.g. 'When saving notes, use D:/notes and Markdown').", {"rule": {"type": "string"}},
                 ["rule"], save_preference, "memory"))
    reg.add(Tool("forget", "Delete remembered facts or preferences containing this text.",
                 {"text": {"type": "string"}}, ["text"], forget, "memory"))
