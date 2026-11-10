"""MCP (Model Context Protocol) client for stdio servers, plus tools to add and remove servers.

Spec-conformant stdio transport: one JSON-RPC message per line (no Content-Length headers), with a
reader thread so a silent server times out instead of hanging the agent.

Config: .omni/mcp.json (kept out of git because env values can hold keys)
    {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/notes"],
                           "env": {"TOKEN": "${MY_TOKEN}"}, "description": "read my notes", "confirm": "edit"}}}
"${VAR}" in env values is filled from your environment, so secrets never need to be written in the file.
Each server's tools form a toolset "mcp:<name>" that the agent switches on when it needs it.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .registry import Tool
from .util import STATE, clip

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    pass


class McpServer:
    def __init__(self, name: str, config: dict, timeout: float = 60):
        self.name, self.config, self.timeout = name, config, timeout
        self.proc: subprocess.Popen | None = None
        self.next_id = 0
        self.lock = threading.Lock()
        self.inbox: queue.Queue = queue.Queue()
        self.tools: list[dict] = []
        self.instructions = ""

    def connect(self):
        cmd = self.config.get("command")
        args = self.config.get("args", [])
        if not isinstance(cmd, str) or not cmd:
            raise McpError("config needs a 'command'")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise McpError("'args' must be a list of strings")
        exe = shutil.which(cmd) or cmd          # finds npx.cmd / uvx.exe on Windows
        env = os.environ.copy()
        for k, v in (self.config.get("env") or {}).items():
            env[str(k)] = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), str(v))
        (STATE / "logs").mkdir(parents=True, exist_ok=True)
        self._stderr = open(STATE / "logs" / f"mcp-{self.name}.log", "ab")
        self.proc = subprocess.Popen([exe, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, env=env, cwd=self.config.get("cwd") or None)
        threading.Thread(target=self._reader, daemon=True).start()
        init = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "omni-agent", "version": "1.0"}})
        self.instructions = str(init.get("instructions") or "")
        self.notify("notifications/initialized", {})
        self.tools, cursor = [], None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            self.tools += [t for t in page.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = page.get("nextCursor")
            if not cursor:
                break

    def _reader(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue                       # servers sometimes print logs to stdout; skip them
            if "method" in msg and "id" in msg:  # a request from the server (ping, roots/list...)
                result = {} if msg["method"] == "ping" else None
                reply = ({"jsonrpc": "2.0", "id": msg["id"], "result": result} if result is not None else
                         {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
                try:
                    self._send(reply)
                except OSError:
                    pass
            elif "id" in msg:
                self.inbox.put(msg)
        self.inbox.put(None)                   # EOF: the server exited

    def _send(self, msg: dict):
        if not self.proc or not self.proc.stdin:
            raise McpError(f"{self.name} is not running")
        with self.lock:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            try:
                msg = self.inbox.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise McpError(f"{self.name}: no reply to {method} within {timeout or self.timeout}s") from None
            if msg is None:
                raise McpError(f"{self.name} exited (see .omni/logs/mcp-{self.name}.log)")
            if msg.get("id") != rid:
                continue                       # late reply to an earlier, timed-out request
            if "error" in msg:
                raise McpError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
            return msg.get("result") or {}

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call_tool(self, tool: str, arguments: dict) -> tuple[str, bool]:
        res = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout=300)
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                parts.append(str(c.get("resource", {}).get("text", c.get("resource", {}).get("uri", ""))))
            else:
                parts.append(f"[{c.get('type')} content]")
        if not parts and "structuredContent" in res:
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        return "\n".join(parts) or "(empty result)", bool(res.get("isError"))

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            if self.proc.stdout:
                self.proc.stdout.close()
            self.proc = None
        if getattr(self, "_stderr", None):
            self._stderr.close()


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)[:60]


def register(agent):
    reg = agent.registry
    cfg_path = Path(agent.cfg.mcp_config) if agent.cfg.mcp_config else STATE / "mcp.json"
    servers: dict[str, McpServer] = {}
    errors: dict[str, str] = {}
    reg.add_toolset("mcp", "add, remove and list MCP servers (plugins that give you new tools)",
                    keywords=r"\bmcp\b|model context protocol")

    def read_cfg() -> dict:
        if not cfg_path.is_file():
            return {}
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data.get("mcpServers", {}) if isinstance(data, dict) else {}

    def write_cfg(servers_cfg: dict):
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"mcpServers": servers_cfg}, indent=2), encoding="utf-8")

    def start(name: str, conf: dict) -> str:
        stop(name)
        srv = McpServer(name, conf, float(conf.get("timeout", 60)))
        try:
            srv.connect()
        except Exception as e:
            srv.close()
            errors[name] = f"{type(e).__name__}: {e}"
            return f"{name}: FAILED to start: {errors[name]}"
        errors.pop(name, None)
        servers[name] = srv
        ts = f"mcp:{name}"
        desc = conf.get("description") or srv.instructions[:150] or f"tools from the {name} MCP server"
        reg.add_toolset(ts, f"{desc} ({len(srv.tools)} tools)", keywords=conf.get("keywords", ""))
        level = conf.get("confirm", "edit")
        for t in srv.tools:
            schema = t.get("inputSchema") or {}

            def call(_srv=srv, _tool=t["name"], **kw):
                return _srv.call_tool(_tool, kw)
            reg.add(Tool(f"{safe_name(name)}__{safe_name(t['name'])}", (t.get("description") or "")[:400],
                         schema.get("properties", {}), schema.get("required", []), call, ts,
                         level if level in ("never", "edit", "act", "always") else "edit"))
        return f"{name}: connected, {len(srv.tools)} tools: " + ", ".join(t["name"] for t in srv.tools)[:500]

    def stop(name: str):
        if name in servers:
            servers.pop(name).close()
        reg.remove_toolset(f"mcp:{name}")

    def start_all():
        try:
            conf = read_cfg()
        except ValueError as e:
            errors["(config)"] = f"{cfg_path} is not valid JSON: {e}"
            return
        for name, c in conf.items():
            if isinstance(c, dict) and not c.get("disabled"):
                agent.print(f"[mcp] {start(name, c)}")

    def list_mcp_servers():
        conf = read_cfg()
        lines = []
        for name, c in conf.items():
            state = ("running, %d tools" % len(servers[name].tools)) if name in servers else \
                    ("disabled" if c.get("disabled") else f"not running: {errors.get(name, 'unknown')}")
            lines.append(f"- {name}: {c.get('command')} {' '.join(c.get('args', []))} [{state}]")
        return ("\n".join(lines) or f"No MCP servers configured ({cfg_path})."), False

    def add_mcp_server(name: str, command: str, args: list | None = None, env: dict | None = None,
                       description: str = ""):
        if not re.match(r"^[A-Za-z0-9_\-]{1,30}$", name):
            return "name must be 1-30 letters, digits, '-' or '_'.", True
        args = [str(a) for a in (args or [])]
        env = {str(k): str(v) for k, v in (env or {}).items()}
        conf = read_cfg()
        entry = {"command": command, "args": args, "description": description}
        if env:
            entry["env"] = env
        shown = json.dumps({name: entry}, indent=2)
        agent.print(f"\n===== Add MCP server =====\n{shown}\n"
                    "This runs the command above as a program on your computer every time the agent starts.\n"
                    "Only approve packages you trust. Put secrets in env as ${YOUR_ENV_VAR}, not as values.")
        if not agent.confirm(f"Add and start MCP server '{name}'?"):
            return "User declined adding this MCP server.", True
        msg = start(name, entry)
        if name in errors:
            return msg + "\nNot saved to the config. Check the command (is node/uv installed?) and try again.", True
        conf[name] = entry
        write_cfg(conf)
        reg.active.add(f"mcp:{name}")
        return msg + f"\nSaved to {cfg_path}. Its tools are on now.", False

    def remove_mcp_server(name: str):
        conf = read_cfg()
        if name not in conf:
            return f"No MCP server named {name}.", True
        if not agent.confirm(f"Remove MCP server '{name}' from {cfg_path}?"):
            return "User declined.", True
        stop(name)
        conf.pop(name)
        write_cfg(conf)
        return f"Removed {name}.", False

    reg.add(Tool("list_mcp_servers", "List configured MCP servers and their state.", {}, [], list_mcp_servers, "mcp"))
    reg.add(Tool("add_mcp_server",
                 "Install an MCP server so you gain its tools. Example: command 'npx', args "
                 "['-y', '@modelcontextprotocol/server-memory']. The user approves it.",
                 {"name": {"type": "string"}, "command": {"type": "string"},
                  "args": {"type": "array", "items": {"type": "string"}},
                  "env": {"type": "object", "description": "env vars; use ${VAR} to read secrets from the environment"},
                  "description": {"type": "string", "description": "one line: what its tools are for"}},
                 ["name", "command"], add_mcp_server, "mcp"))
    reg.add(Tool("remove_mcp_server", "Remove an MCP server.", {"name": {"type": "string"}}, ["name"],
                 remove_mcp_server, "mcp"))

    agent.mcp_servers, agent.mcp_errors = servers, errors
    agent.closers.append(lambda: [s.close() for s in list(servers.values())])
    start_all()
