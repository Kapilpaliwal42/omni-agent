#!/usr/bin/env python3
"""omni-agent command line: a local-model agent that learns skills, adds MCP servers, uses the web, a
browser and the Windows desktop, and can change its own code when you ask.

Usage:
    .venv\\Scripts\\python agent.py                                   # interactive, workspace = current folder
    .venv\\Scripts\\python agent.py --workspace D:\\work "summarize every PDF name in this folder into list.md"

Interactive commands:
    /improve <what>   change the agent's own code (tested in a copy; you approve the diff)
    /learn [hint]     turn the last task into a reusable skill
    /skills           list learned skills          /mcp       list MCP servers
    /memory           show remembered facts        /history   self-update and skill commits
    /rollback         revert the last self-update or skill commit
    /reset            clear the conversation       /exit      quit
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from omni.commands import is_command, run_command
from omni.core import Agent, Cancelled, Config, OllamaError
from omni.util import ROOT


def build_config(argv=None) -> tuple[Config, list[str]]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="*", help="one-shot task; omit for interactive mode")
    ap.add_argument("--workspace", default=".", help="folder the agent works in")
    ap.add_argument("--rules", default=str(ROOT / "rules.md"), help="your rules / system prompt file")
    ap.add_argument("--mode", choices=["ask", "auto", "yolo"], default="auto",
                    help="ask: confirm every action; auto: confirm risky ones; yolo: confirm only self-changes")
    ap.add_argument("--model", default=os.environ.get("OMNI_MODEL", "huihui_ai/qwen3.5-abliterated:9b"))
    ap.add_argument("--vision-model", default=os.environ.get("OMNI_VISION_MODEL"),
                    help="optional Ollama vision model for describing screenshots, e.g. qwen2.5vl:3b")
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--think", choices=["on", "off"], default="off")
    ap.add_argument("--shell", choices=["powershell", "bash", "sh"], default=None)
    ap.add_argument("--allow-outside", action="store_true", help="let file tools touch paths outside the workspace")
    ap.add_argument("--no-plan", action="store_true", help="one long conversation instead of plan-then-execute")
    ap.add_argument("--verify", default=None, help="check command after tasks that change files")
    ap.add_argument("--fix-attempts", type=int, default=2)
    ap.add_argument("--no-web", action="store_true", help="start with web tools off")
    ap.add_argument("--headless", action="store_true", help="run the browser without a window")
    ap.add_argument("--trust-desktop", action="store_true",
                    help="desktop clicks/typing run without asking in auto mode (risky-looking clicks still ask)")
    ap.add_argument("--searxng", default=os.environ.get("OMNI_SEARXNG_URL"), help="local SearXNG URL for web_search")
    ap.add_argument("--mcp-config", default=None, help="MCP config file (default .omni/mcp.json)")
    ap.add_argument("--no-self-update", action="store_true", help="remove the self_improve tool")
    ap.add_argument("--coder-model", default=os.environ.get("OMNI_CODER_MODEL"),
                    help="model used when the agent changes its own code (default: --model). qwen3.5:9b codes better")
    ap.add_argument("--coder-think", choices=["on", "off"], default=os.environ.get("OMNI_CODER_THINK"),
                    help="thinking for the coder model (slower, often better code)")
    ap.add_argument("--self-attempts", type=int, default=2, help="fresh tries a self-update gets")
    ap.add_argument("--self-fix-rounds", type=int, default=3, help="fix rounds per self-update attempt")
    a = ap.parse_args(argv)
    cfg = Config(workspace=a.workspace, rules=a.rules, mode=a.mode, model=a.model, host=a.host, num_ctx=a.num_ctx,
                 temperature=a.temperature, think=a.think, shell=a.shell, allow_outside=a.allow_outside,
                 use_plan=not a.no_plan, verify=a.verify, fix_attempts=a.fix_attempts, web=not a.no_web,
                 browser_headless=a.headless, vision_model=a.vision_model, searxng_url=a.searxng,
                 mcp_config=a.mcp_config, enable_self_update=not a.no_self_update,
                 coder_model=a.coder_model, coder_think=a.coder_think,
                 extra={"trust_desktop": a.trust_desktop, "self_attempts": a.self_attempts,
                        "self_fix_rounds": a.self_fix_rounds})
    return cfg, a.task


def main():
    cfg, task = build_config()
    agent = Agent(cfg)
    print(f"omni-agent | workspace={agent.workspace} | mode={cfg.mode} | model={cfg.model} | "
          f"skills={len(agent.skills)} | mcp={len(agent.mcp_servers)} | log={agent.log_path}")

    def safe(fn, *a):
        try:
            return fn(*a)
        except KeyboardInterrupt:
            print("\n[interrupted]")
        except OllamaError as e:
            print(f"[ollama error] {e}")

    def maybe_restart():
        if agent.restart_requested:
            print("\n[restarting to load the updated code]")
            agent.close()
            sys.exit(subprocess.call([sys.executable, *sys.argv]))

    try:
        if task:
            safe(agent.run, " ".join(task))
            maybe_restart()
            return
        while True:
            try:
                text = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text in ("/exit", "/quit"):
                break
            elif is_command(text):
                print(safe(run_command, agent, text) or "")
            elif text:
                safe(agent.run, text)
            maybe_restart()
    finally:
        agent.close()


if __name__ == "__main__":
    main()
