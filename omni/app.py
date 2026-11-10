"""The omni-agent app: a local web server plus a chat UI (omni/web/index.html), opened as an Edge app window.

Security: the server listens on 127.0.0.1 only. Every request must carry a random per-launch token, and
the Host header must be localhost, so other websites open in your browser can't drive the agent
(no CSRF, no DNS rebinding).

Protocol:
    GET  /?t=TOKEN               the UI
    GET  /api/events?t=&since=N  server-sent events: everything the agent does, in order
    GET  /api/state              settings, skills, MCP servers, memory, models
    POST /api/send     {text}    run a task or a /command
    POST /api/confirm  {id, allow, always}   answer a permission prompt ("always" = for this session)
    POST /api/answer   {id, text}            answer a question the agent asked
    POST /api/schedule {id, action}          pause | resume | run | delete a scheduled task
    POST /api/allowed                        clear the "always allow" list
    POST /api/stop               stop the running task (pending confirmations are denied)
    POST /api/settings {mode, model, plan, workspace}
    POST /api/new                new conversation
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from .commands import COMMANDS, is_command, run_command
from .core import Agent, Cancelled, OllamaError
from .util import STATE, redact

WEB = Path(__file__).resolve().parent / "web"


class Hub:
    """Ordered event log with blocking reads, so each browser connection can stream from any point."""

    def __init__(self):
        self.events: list[dict] = []
        self.cond = threading.Condition()

    def push(self, kind: str, **data):
        with self.cond:
            ev = {"seq": len(self.events) + 1, "kind": kind, "ts": time.time(), **data}
            self.events.append(json.loads(redact(json.dumps(ev, ensure_ascii=False, default=str))))
            self.cond.notify_all()

    def since(self, seq: int, timeout: float) -> list[dict]:
        with self.cond:
            if seq > len(self.events):     # client remembers a previous server instance
                seq = 0
            if len(self.events) <= seq:
                self.cond.wait(timeout)
            return self.events[seq:]

    def clear(self):
        with self.cond:
            self.events.clear()
            self.cond.notify_all()


class Session:
    def __init__(self, cfg, restart_argv: list[str], chat_fn=None, modules=None, scheduler: bool = True):
        self.hub = Hub()
        self.instance = secrets.token_hex(4)
        self.restart_argv = restart_argv
        self.agent = Agent(cfg, ui_print=lambda *a: self.hub.push("log", text=" ".join(map(str, a)).strip("\n")),
                           chat_fn=chat_fn, modules=modules)
        self.agent.structured = True
        self.agent.emit = self.hub.push
        self.agent.confirm_fn = self.ask
        self.agent.ask_fn = self.question
        self.busy = False
        self.lock = threading.Lock()
        self.pending: dict[int, dict] = {}       # open permission prompts and questions, by id
        self.next_id = 0
        self._models: tuple[float, list[str]] = (0.0, [])
        self.on_restart = None
        self.running_schedule: str | None = None
        if scheduler and hasattr(self.agent, "schedules"):
            threading.Thread(target=self._scheduler, daemon=True).start()

    # ---- the agent thread blocks in these until the UI answers (Stop answers "no" / "")
    def _wait(self, kind: str, **data) -> dict:
        with self.lock:
            self.next_id += 1
            cid = self.next_id
            slot = {"event": threading.Event(), "kind": kind, "allow": False, "always": False, "text": ""}
            self.pending[cid] = slot
        self.hub.push(kind, id=cid, **data)
        slot["event"].wait()
        self.pending.pop(cid, None)
        return slot | {"id": cid}

    def ask(self, text: str, allow_key: str | None = None):
        s = self._wait("confirm", text=text, allow_key=allow_key,
                       always_label=self.always_label(allow_key) if allow_key else "")
        self.hub.push("confirm_answer", id=s["id"], allow=s["allow"], always=s["always"] and s["allow"])
        return "always" if s["allow"] and s["always"] and allow_key else s["allow"]

    @staticmethod
    def always_label(key: str) -> str:
        if key.startswith("run_command:"):
            return f"Always allow `{key.split(':', 1)[1]}` commands"
        return {"run_command": "Always allow commands", "write_file": "Always allow file writes",
                "edit_file": "Always allow file edits"}.get(key, f"Always allow {key}")

    def question(self, text: str) -> str:
        s = self._wait("question", text=text)
        self.hub.push("question_answer", id=s["id"], text=s["text"])
        return s["text"]

    def answer(self, cid: int, allow: bool = False, always: bool = False, text: str = "") -> bool:
        slot = self.pending.get(cid)
        if not slot:
            return False
        slot.update(allow=allow, always=always, text=str(text)[:4000])
        slot["event"].set()
        return True

    # ---- tasks
    def send(self, text: str, schedule: dict | None = None) -> tuple[bool, str]:
        with self.lock:
            if self.busy:
                return False, "The agent is busy. Wait, or press Stop."
            self.busy = True
        self.agent.cancelled = False
        threading.Thread(target=self._work, args=(text, schedule), daemon=True).start()
        return True, "started"

    def _work(self, text: str, schedule: dict | None = None):
        a = self.agent
        saved_ws = a.workspace
        status = "done"
        if schedule:
            self.running_schedule = schedule["id"]
            ws = Path(schedule.get("workspace") or a.workspace)
            if ws.is_dir():
                a.workspace = ws.resolve()
        self.hub.push("user", text=text, schedule={"id": schedule["id"], "when": schedule["when"]} if schedule else None)
        self.hub.push("busy", busy=True)
        try:
            if is_command(text):
                out = run_command(a, text)
                if out:
                    self.hub.push("command_result", command=text.split()[0], text=out)
            else:
                res = a.run(text)
                status = res.get("status", "done")
        except Cancelled:
            status = "stopped"
            self.hub.push("stopped")
        except OllamaError as e:
            status = "error"
            self.hub.push("error", text=f"Ollama: {e}")
        except Exception as e:
            status = "error"
            self.hub.push("error", text=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-3000:])
        finally:
            if schedule:
                a.schedules.finished(schedule["id"], status)
                a.workspace = saved_ws
                self.running_schedule = None
                self.hub.push("schedules_changed")
            self.busy = False
            a.cancelled = False
            self.hub.push("busy", busy=False)
            if text.startswith("/"):
                self.hub.push("state_changed")
            if a.restart_requested and self.on_restart:
                self.hub.push("restarting")
                time.sleep(0.5)
                self.on_restart()

    def _scheduler(self, every: float = 15.0):
        """Start due scheduled tasks, one at a time, whenever the agent is free."""
        while True:
            time.sleep(every)
            self.run_due()

    def run_due(self) -> bool:
        if self.busy:
            return False
        try:
            due = self.agent.schedules.due()
        except Exception:
            return False
        if not due:
            return False
        it = due[0]
        self.agent.schedules.started(it["id"])     # advance first: a crash never re-runs it in a loop
        ok, _ = self.send(it["task"], schedule=it)
        return ok

    def stop(self):
        self.agent.cancelled = True
        for cid in list(self.pending):
            self.answer(cid, False)

    # ---- state for the sidebar
    def models(self) -> list[str]:
        ts, cached = self._models
        if time.time() - ts > 30:
            try:
                with urlopen(self.agent.cfg.host.rstrip("/") + "/api/tags", timeout=3) as r:
                    cached = sorted(m["name"] for m in json.load(r).get("models", []))
            except Exception:
                cached = cached or []
            self._models = (time.time(), cached)
        return cached

    def state(self) -> dict:
        a = self.agent
        mcp = []
        for name, srv in getattr(a, "mcp_servers", {}).items():
            mcp.append({"name": name, "ok": True, "tools": len(srv.tools)})
        for name, err in getattr(a, "mcp_errors", {}).items():
            mcp.append({"name": name, "ok": False, "error": err})
        mem = STATE / "memory.md"
        sched = a.schedules.load() if hasattr(a, "schedules") else []
        return {
            "instance": self.instance, "busy": self.busy, "workspace": str(a.workspace), "mode": a.mode,
            "model": a.cfg.model, "plan": a.cfg.use_plan, "models": self.models(),
            "skills": [{"name": s["name"], "description": s["description"], "tools": s["tools"],
                        "error": s["error"]} for s in getattr(a, "skills", {}).values()],
            "mcp": mcp,
            "memory": [l[2:] for l in mem.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
                      if mem.is_file() else [],
            "preferences": [l[2:] for l in getattr(a, "preference_lines", lambda: [])()],
            "schedules": [{k: it.get(k) for k in ("id", "task", "when", "enabled", "next_run", "last_run",
                                                   "last_status")} | {"running": it["id"] == self.running_schedule}
                          for it in sched],
            "allowed": [{"key": k, "label": self.always_label(k)} for k in sorted(a.session_allow)],
            "has_last_task": bool(getattr(a, "last_task", None)),
            "toolsets": [{"name": t.name, "description": t.description} for t in a.registry.toolsets.values()
                         if not t.name.startswith(("skill:", "mcp:"))],
            "commands": COMMANDS, "self_update": hasattr(a, "self_improve"),
            "pending": [{"id": k, "kind": v["kind"]} for k, v in self.pending.items()],
        }

    def schedule_action(self, sid: str, action: str) -> tuple[bool, str]:
        s = getattr(self.agent, "schedules", None)
        if not s:
            return False, "Scheduling is off."
        if action == "delete":
            ok = s.remove(sid)
        elif action in ("pause", "resume"):
            ok = s.update(sid, enabled=action == "resume") is not None
        elif action == "run":
            items = [it for it in s.load() if it["id"] == sid]
            if not items:
                return False, "No such schedule."
            ok, msg = self.send(items[0]["task"], schedule=items[0])
            if not ok:
                return False, msg
        else:
            return False, "Unknown action."
        self.hub.push("schedules_changed")
        return ok, "ok" if ok else "No such schedule."

    def settings(self, data: dict) -> tuple[bool, str]:
        if self.busy:
            return False, "Can't change settings while a task runs."
        a = self.agent
        if data.get("mode") in ("ask", "auto", "yolo"):
            a.mode = a.cfg.mode = data["mode"]
        if isinstance(data.get("model"), str) and data["model"].strip():
            a.cfg.model = data["model"].strip()
        if isinstance(data.get("plan"), bool):
            a.cfg.use_plan = data["plan"]
        if isinstance(data.get("workspace"), str) and data["workspace"].strip():
            p = Path(data["workspace"].strip()).expanduser()
            if not p.is_dir():
                return False, f"Folder not found: {p}"
            a.workspace = p.resolve()
            a.cfg.workspace = str(a.workspace)
        self.hub.push("settings", mode=a.mode, model=a.cfg.model, plan=a.cfg.use_plan, workspace=str(a.workspace))
        return True, "ok"


def make_handler(session: Session, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _ok_origin(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                return False
            q = parse_qs(urlparse(self.path).query)
            got = self.headers.get("X-Omni-Token") or (q.get("t") or [""])[0]
            return secrets.compare_digest(got, token)

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode())

        def do_GET(self):
            path = urlparse(self.path).path
            if not self._ok_origin():
                return self._send(403, b"Forbidden: open the link printed by app.py (it carries the access token).",
                                  "text/plain")
            if path in ("/", "/index.html"):
                html = (WEB / "index.html").read_bytes().replace(b"__OMNI_TOKEN__", token.encode())
                return self._send(200, html, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if path == "/api/state":
                return self._json(200, session.state())
            if path == "/api/events":
                return self._events()
            self._send(404, b"not found", "text/plain")

        def _events(self):
            q = parse_qs(urlparse(self.path).query)
            try:
                seq = int(self.headers.get("Last-Event-ID") or (q.get("since") or ["0"])[0])
            except ValueError:
                seq = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                hello = {"kind": "hello", "instance": session.instance, "busy": session.busy}
                self.wfile.write(f"event: hello\ndata: {json.dumps(hello)}\n\n".encode())
                self.wfile.flush()
                if seq > len(session.hub.events):
                    seq = 0
                while True:
                    evs = session.hub.since(seq, 15)
                    if not evs:
                        self.wfile.write(b": ping\n\n")
                    for ev in evs:
                        self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                        seq = ev["seq"]
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

        def do_POST(self):
            try:   # read the body first, even for a rejected request, so the reply isn't a connection reset
                n = min(int(self.headers.get("Content-Length") or 0), 2_000_000)
                body = self.rfile.read(n) if n else b""
            except ValueError:
                return self._json(400, {"error": "bad length"})
            if not self._ok_origin():
                return self._json(403, {"error": "forbidden"})
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            if not isinstance(data, dict):
                return self._json(400, {"error": "expected a JSON object"})
            path = urlparse(self.path).path
            if path == "/api/send":
                text = str(data.get("text", "")).strip()
                if not text:
                    return self._json(400, {"error": "empty"})
                ok, msg = session.send(text)
                return self._json(200 if ok else 409, {"ok": ok, "message": msg})
            if path == "/api/confirm":
                return self._json(200, {"ok": session.answer(int(data.get("id", 0)), bool(data.get("allow")),
                                                             bool(data.get("always")))})
            if path == "/api/answer":
                return self._json(200, {"ok": session.answer(int(data.get("id", 0)), text=str(data.get("text", "")))})
            if path == "/api/schedule":
                ok, msg = session.schedule_action(str(data.get("id", "")), str(data.get("action", "")))
                return self._json(200 if ok else 409, {"ok": ok, "message": msg})
            if path == "/api/allowed":
                session.agent.session_allow.clear()
                session.hub.push("allow_list", keys=[])
                return self._json(200, {"ok": True})
            if path == "/api/stop":
                session.stop()
                return self._json(200, {"ok": True})
            if path == "/api/settings":
                ok, msg = session.settings(data)
                return self._json(200 if ok else 409, {"ok": ok, "message": msg})
            if path == "/api/new":
                if session.busy:
                    return self._json(409, {"ok": False, "message": "busy"})
                session.agent.messages.clear()
                session.agent.history.clear()
                session.hub.clear()
                session.hub.push("cleared")
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})

    return H


def find_edge() -> str | None:
    for c in (shutil.which("msedge"),
              os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")):
        if c and Path(c).is_file():
            return c
    return None


def open_window(url: str):
    """An app-style window (no tabs or address bar) with its own profile, like a desktop app."""
    edge = find_edge()
    if edge:
        prof = STATE / "app-window"
        prof.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1280,880",
                          "--no-first-run", "--no-default-browser-check"])
    else:
        import webbrowser
        webbrowser.open(url)


def serve(cfg, port: int = 8765, open_ui: bool = True, argv: list[str] | None = None, chat_fn=None):
    token = os.environ.get("OMNI_TOKEN") or secrets.token_urlsafe(24)
    os.environ["OMNI_TOKEN"] = token          # a self-update restart keeps the same token, so the window reconnects
    session = Session(cfg, argv or sys.argv, chat_fn=chat_fn)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session, token, port))
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/?t={token}"

    def restart():
        session.agent.close()
        srv.shutdown()
        srv.server_close()
        args = [a for a in session.restart_argv if a != "--no-open"] + ["--no-open"]
        subprocess.Popen([sys.executable, *args], env=os.environ.copy())
        os._exit(0)
    session.on_restart = lambda: threading.Thread(target=restart, daemon=True).start()

    print(f"omni-agent app running at {url}\n(keep this window open; Ctrl+C quits)")
    if open_ui:
        open_window(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.agent.close()
