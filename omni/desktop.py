"""Windows desktop control through UI Automation (pywinauto), with pyautogui/PIL for screenshots.

Like the browser, windows come back as numbered controls ([4] Button "Save") so the model acts by
number instead of guessing pixel coordinates. A vision model (--vision-model) can additionally describe
screenshots for apps that expose no UI Automation tree (games, some Electron or Java apps).

Every desktop action asks first in `auto` mode unless the agent was started with --trust-desktop;
even then, clicks on controls named like Delete/Send/Pay/... still ask.
"""
from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path

from .browser import RISKY_CLICK
from .registry import Tool
from .util import clip

INTERACTIVE = {"Button", "Edit", "MenuItem", "ListItem", "TabItem", "CheckBox", "ComboBox", "Hyperlink",
               "TreeItem", "RadioButton", "Document", "SplitButton", "DataItem", "Slider", "Spinner"}
KEY_NAMES = {"enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}", "escape": "{ESC}",
             "delete": "{DEL}", "del": "{DEL}", "backspace": "{BACKSPACE}", "space": "{SPACE}", "up": "{UP}",
             "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}", "home": "{HOME}", "end": "{END}",
             "pageup": "{PGUP}", "pgup": "{PGUP}", "pagedown": "{PGDN}", "pgdn": "{PGDN}", "insert": "{INSERT}",
             **{f"f{i}": f"{{F{i}}}" for i in range(1, 13)}}
MODS = {"ctrl": "^", "control": "^", "shift": "+", "alt": "%"}


def escape_text(text: str) -> str:
    """Literal text -> pywinauto send_keys syntax."""
    out = re.sub(r"([+^%~(){}\[\]])", r"{\1}", text)
    return out.replace("\r\n", "\n").replace("\n", "{ENTER}").replace("\t", "{TAB}")


def hotkey_to_keys(combo: str) -> str:
    """'ctrl+shift+s' -> '^+s', 'alt+f4' -> '%{F4}', 'win+r' -> '{VK_LWIN down}r{VK_LWIN up}'."""
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    k = KEY_NAMES.get(key) or (escape_text(key) if len(key) == 1 else None)
    if k is None:
        raise ValueError(f"unknown key {key!r}")
    prefix, win = "", False
    for m in mods:
        if m in ("win", "windows", "meta", "super"):
            win = True
        elif m in MODS:
            prefix += MODS[m]
        else:
            raise ValueError(f"unknown modifier {m!r}")
    keys = prefix + k
    return "{VK_LWIN down}" + keys + "{VK_LWIN up}" if win else keys


def register(agent):
    reg = agent.registry
    trust = bool(agent.cfg.extra.get("trust_desktop"))
    reg.add_toolset("desktop", "control Windows apps: list windows, read their buttons and fields, click, type, "
                    "hotkeys, launch programs, screenshots",
                    keywords=r"\b(desktop|window|notepad|excel|word|explorer|settings|calculator|paint|"
                             r"application|app|my (pc|computer|laptop)|screen)\b")
    state: dict = {"refs": {}, "lines": {}, "window": None}

    def desktop():
        from pywinauto import Desktop   # optional dependency, imported on first use
        return Desktop(backend="uia")

    def send_keys(keys: str):
        from pywinauto.keyboard import send_keys as sk
        sk(keys, with_spaces=True, pause=0.02)

    def windows():
        return [w for w in desktop().windows() if w.window_text().strip() and w.is_visible()]

    def desktop_windows():
        ws = windows()
        lines = [f"[{i}] {w.window_text()[:100]}  ({w.element_info.class_name})" for i, w in enumerate(ws, 1)]
        return ("\n".join(lines) or "(no visible windows)"), False

    def find_window(window: str):
        ws = windows()
        if str(window).isdigit() and 1 <= int(window) <= len(ws):
            return ws[int(window) - 1]
        hits = [w for w in ws if str(window).lower() in w.window_text().lower()]
        if not hits:
            raise LookupError(f"No window matching {window!r}. Call desktop_windows to see what is open.")
        return hits[0]

    def snapshot(win) -> str:
        refs, lines, out = {}, {}, []
        try:
            ctrls = win.descendants()
        except Exception as e:
            return f"Could not read the controls of this window ({e}). Try desktop_screenshot."
        n = 0
        for c in ctrls:
            try:
                ct = c.element_info.control_type
                if ct not in INTERACTIVE or not c.is_visible():
                    continue
                name = (c.window_text() or c.element_info.name or c.element_info.automation_id or "").strip()
                name = re.sub(r"\s+", " ", name)[:80]
                val = ""
                if ct in ("Edit", "Document", "ComboBox"):
                    try:
                        v = c.get_value() if hasattr(c, "get_value") else ""
                        val = f' value="{str(v)[:60]}"' if v and str(v) != name else ""
                    except Exception:
                        pass
            except Exception:
                continue
            n += 1
            refs[n] = c
            lines[n] = f"[{n}] {ct} \"{name}\"{val}"
            out.append(lines[n])
            if n >= 150:
                out.append("... (more controls not shown)")
                break
        state.update(refs=refs, lines=lines, window=win)
        return f"Window: {win.window_text()}\nControls:\n" + ("\n".join(out) or "(none exposed; try desktop_screenshot)")

    def desktop_snapshot(window: str):
        win = find_window(window)
        try:
            win.set_focus()
        except Exception:
            pass
        return snapshot(win), False

    def ctrl(ref):
        c = state["refs"].get(int(ref))
        if c is None:
            raise LookupError(f"No control [{ref}]. Call desktop_snapshot for fresh numbers.")
        return c

    def refreshed(msg: str):
        time.sleep(0.5)
        win = state["window"]
        return (f"{msg}\n\n{snapshot(win)}" if win else msg), False

    def desktop_click(ref: int, double: bool = False):
        c = ctrl(ref)
        (c.double_click_input if double else c.click_input)()
        return refreshed(f"{'Double-clicked' if double else 'Clicked'} [{ref}]")

    def desktop_type(text: str, ref: int | None = None, enter: bool = False):
        if ref is not None:
            c = ctrl(ref)
            try:
                c.set_focus()
            except Exception:
                c.click_input()
        send_keys(escape_text(text) + ("{ENTER}" if enter else ""))
        return refreshed(f"Typed {len(text)} chars" + (f" into [{ref}]" if ref is not None else ""))

    def desktop_hotkey(keys: str):
        send_keys(hotkey_to_keys(keys))
        return refreshed(f"Pressed {keys}")

    def desktop_launch(program: str):
        os.startfile(program)          # notepad, calc, an .exe path, a file, or a URI like ms-settings:
        time.sleep(2.0)
        return f"Started {program}. Call desktop_windows, then desktop_snapshot on its window.", False

    def desktop_read(ref: int):
        c = ctrl(ref)
        texts = []
        for get in (lambda: c.get_value(), lambda: c.window_text(), lambda: "\n".join(c.texts())):
            try:
                if (t := get()) and t not in texts:
                    texts.append(str(t))
            except Exception:
                pass
        return clip("\n".join(texts) or "(no text)", 6000), False

    def desktop_screenshot(path: str = "screen.png", question: str = ""):
        from PIL import ImageGrab
        f = agent.resolve(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(all_screens=False).save(f)
        out = f"Saved screenshot to {path}."
        if question:
            out += "\n" + (describe_image(f, question) if agent.cfg.vision_model else
                           "(no --vision-model configured, so the image was not described)")
        return out, False

    def describe_image(path: Path, question: str) -> str:
        img = base64.b64encode(Path(path).read_bytes()).decode()
        data = agent.chat([{"role": "user", "content": question, "images": [img]}], tools=False,
                          system="Describe what is on the screen precisely. List visible buttons and text with "
                                 "their approximate position (top-left, center...).",
                          model=agent.cfg.vision_model)
        return "Vision model: " + (data.get("message", {}).get("content") or "").strip()

    if agent.cfg.vision_model:
        agent.describe_image = describe_image

    def risky(args):
        line = state["lines"].get(int(args.get("ref") or 0), "")
        return f"click on {line[:100]}" if RISKY_CLICK.search(line) else None

    level = "never" if trust else "act"
    T = lambda *a, **k: reg.add(Tool(*a, toolset="desktop", **k))
    T("desktop_windows", "List the open windows, numbered.", {}, [], desktop_windows)
    T("desktop_snapshot", "Focus a window (number or part of its title) and list its controls, numbered.",
      {"window": {"type": "string"}}, ["window"], desktop_snapshot)
    T("desktop_click", "Click control ref from the last desktop_snapshot. double=true double-clicks.",
      {"ref": {"type": "integer"}, "double": {"type": "boolean"}}, ["ref"], desktop_click, confirm=level, guard=risky)
    T("desktop_type", "Type text, into control ref if given (else wherever the focus is). enter=true presses Enter.",
      {"text": {"type": "string"}, "ref": {"type": "integer"}, "enter": {"type": "boolean"}}, ["text"],
      desktop_type, confirm=level)
    T("desktop_hotkey", "Press a key combo like ctrl+s, alt+f4, win+r, ctrl+shift+esc, enter.",
      {"keys": {"type": "string"}}, ["keys"], desktop_hotkey, confirm=level)
    T("desktop_launch", "Start a program, file or URI: 'notepad', 'calc', a path, 'ms-settings:'.",
      {"program": {"type": "string"}}, ["program"], desktop_launch, confirm="act")
    T("desktop_read", "Read the full text of control ref (e.g. a document or text box).",
      {"ref": {"type": "integer"}}, ["ref"], desktop_read)
    T("desktop_screenshot", "Save a screenshot of the screen into the workspace; with question (and a vision "
      "model) also describes it.", {"path": {"type": "string"}, "question": {"type": "string"}}, [], desktop_screenshot)
