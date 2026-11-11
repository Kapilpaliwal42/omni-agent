#!/usr/bin/env python3
"""Start omni-agent as a desktop-style app (a local server + an Edge app window).

    .venv\\Scripts\\python app.py                         # opens the app window
    .venv\\Scripts\\python app.py --workspace D:\\work --mode ask --port 8765
    .venv\\Scripts\\python app.py --no-open               # server only; open the printed link yourself

Takes every flag that agent.py takes (model, mode, workspace, vision model, ...), plus --port, --no-open, and
--browser-window (also show the agent's browser as a separate window; by default it lives in the app's panel).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import build_config          # noqa: E402
from omni.app import serve              # noqa: E402


def main():
    argv = sys.argv[1:]
    port, open_ui, rest, browser_window = 8765, True, [], False
    i = 0
    while i < len(argv):
        if argv[i] == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
            continue
        if argv[i] == "--no-open":
            open_ui = False
        elif argv[i] == "--browser-window":    # also show the agent's browser as its own window
            browser_window = True
        else:
            rest.append(argv[i])
        i += 1
    cfg, _ = build_config(rest)
    if not browser_window:                    # the app shows the browser in its own panel
        cfg.browser_headless = True
    serve(cfg, port=port, open_ui=open_ui, argv=[os.path.abspath(__file__), *argv])


if __name__ == "__main__":
    main()
