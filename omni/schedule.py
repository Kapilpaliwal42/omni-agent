"""Scheduled and recurring tasks, stored in .omni/schedules.json and run by the app (app.py) while it is open.

When formats (case-insensitive):
    every 30m | every 2h | hourly          repeating interval
    daily 09:00 | every day at 9:00        once a day
    weekdays 18:30                         Monday to Friday
    mon,wed,fri 07:15                      chosen days
    once 2026-11-20 14:00 | in 20m | in 2h one time
Creating a schedule always asks the user, because it is a standing instruction that runs unattended later.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import threading
from pathlib import Path

from .registry import Tool
from .util import STATE

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _hm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s.strip())
    if not m:
        raise ValueError(f"bad time {s!r} (use HH:MM)")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError(f"bad time {s!r}")
    return h, mi


def parse_when(spec: str, now: dt.datetime | None = None) -> dict:
    """Normalize a when-string into {"type": interval|daily|days|once, ...}. Raises ValueError."""
    now = now or dt.datetime.now()
    s = " ".join(spec.lower().replace(" at ", " ").split())
    if m := re.fullmatch(r"(?:every\s+)?(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)", s):
        n = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        if n < 5:
            raise ValueError("the shortest interval is 5 minutes")
        return {"type": "interval", "minutes": n}
    if s in ("hourly", "every hour"):
        return {"type": "interval", "minutes": 60}
    if m := re.fullmatch(r"in\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)", s):
        n = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        return {"type": "once", "at": (now + dt.timedelta(minutes=n)).isoformat(timespec="minutes")}
    if m := re.fullmatch(r"once\s+(\d{4}-\d{2}-\d{2})\s+(.+)", s):
        h, mi = _hm(m.group(2))
        return {"type": "once", "at": f"{m.group(1)}T{h:02d}:{mi:02d}"}
    if m := re.fullmatch(r"(?:daily|every day|each day|everyday)\s+(.+)", s):
        h, mi = _hm(m.group(1))
        return {"type": "days", "days": list(range(7)), "time": f"{h:02d}:{mi:02d}"}
    if m := re.fullmatch(r"(?:weekdays|every weekday)\s+(.+)", s):
        h, mi = _hm(m.group(1))
        return {"type": "days", "days": [0, 1, 2, 3, 4], "time": f"{h:02d}:{mi:02d}"}
    if m := re.fullmatch(r"(?:every\s+)?((?:mon|tue|wed|thu|fri|sat|sun)[a-z]*(?:\s*,\s*(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)*)\s+(.+)", s):
        days = sorted({DAYS.index(d.strip()[:3]) for d in m.group(1).split(",")})
        h, mi = _hm(m.group(2))
        return {"type": "days", "days": days, "time": f"{h:02d}:{mi:02d}"}
    raise ValueError(f"can't understand when={spec!r}. Use e.g. 'every 30m', 'daily 09:00', 'weekdays 18:30', "
                     "'mon,fri 07:00', 'once 2026-11-20 14:00' or 'in 20m'.")


def next_run(rule: dict, after: dt.datetime) -> dt.datetime | None:
    if rule["type"] == "interval":
        return after + dt.timedelta(minutes=rule["minutes"])
    if rule["type"] == "once":
        at = dt.datetime.fromisoformat(rule["at"])
        return at if at > after else None
    h, mi = map(int, rule["time"].split(":"))
    for k in range(0, 8):
        day = (after + dt.timedelta(days=k)).replace(hour=h, minute=mi, second=0, microsecond=0)
        if day > after and day.weekday() in rule["days"]:
            return day
    return None


def describe(rule: dict) -> str:
    if rule["type"] == "interval":
        m = rule["minutes"]
        return f"every {m // 60}h" if m % 60 == 0 else f"every {m}m"
    if rule["type"] == "once":
        return "once at " + rule["at"].replace("T", " ")
    days = rule["days"]
    label = "daily" if len(days) == 7 else "weekdays" if days == [0, 1, 2, 3, 4] else ",".join(DAYS[d] for d in days)
    return f"{label} {rule['time']}"


class Schedules:
    def __init__(self, path: Path | None = None):
        self.path = path or STATE / "schedules.json"
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save(self, items: list[dict]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, task: str, when: str, workspace: str, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now()
        rule = parse_when(when, now)
        nxt = next_run(rule, now)
        if not nxt:
            raise ValueError("that time is already in the past")
        item = {"id": secrets.token_hex(3), "task": task.strip(), "when": describe(rule), "rule": rule,
                "workspace": workspace, "enabled": True, "next_run": nxt.isoformat(timespec="minutes"),
                "last_run": None, "last_status": None, "created": now.isoformat(timespec="minutes")}
        with self.lock:
            items = self.load()
            items.append(item)
            self.save(items)
        return item

    def update(self, sid: str, **fields) -> dict | None:
        with self.lock:
            items = self.load()
            for it in items:
                if it["id"] == sid:
                    it.update(fields)
                    if fields.get("enabled") and not it.get("next_run"):
                        nxt = next_run(it["rule"], dt.datetime.now())
                        it["next_run"] = nxt.isoformat(timespec="minutes") if nxt else None
                    self.save(items)
                    return it
        return None

    def remove(self, sid: str) -> bool:
        with self.lock:
            items = self.load()
            keep = [it for it in items if it["id"] != sid]
            self.save(keep)
            return len(keep) != len(items)

    def due(self, now: dt.datetime | None = None) -> list[dict]:
        now = now or dt.datetime.now()
        return [it for it in self.load() if it.get("enabled") and it.get("next_run")
                and dt.datetime.fromisoformat(it["next_run"]) <= now]

    def started(self, sid: str, now: dt.datetime | None = None):
        """Advance to the next time before running, so a crash mid-run never repeats it in a loop."""
        now = now or dt.datetime.now()
        with self.lock:
            items = self.load()
            for it in items:
                if it["id"] == sid:
                    nxt = next_run(it["rule"], now)
                    it["last_run"] = now.isoformat(timespec="minutes")
                    it["next_run"] = nxt.isoformat(timespec="minutes") if nxt else None
                    if not nxt:
                        it["enabled"] = False
            self.save(items)

    def finished(self, sid: str, status: str):
        self.update(sid, last_status=status)


def register(agent):
    reg = agent.registry
    sched = Schedules()
    agent.schedules = sched
    reg.add_toolset("schedule", "run tasks later or on a repeating schedule (every 30m, daily 09:00, weekdays...)",
                    keywords=r"\b(every (day|morning|evening|night|week|hour|\d+)|daily|hourly|weekly|weekdays|"
                             r"schedul\w*|remind me|each (morning|day|week)|at \d{1,2}(:\d\d)?\s*(am|pm)?|tomorrow)\b")

    def schedule_task(task: str, when: str):
        try:
            rule = parse_when(when)
        except ValueError as e:
            return str(e), True
        if not agent.confirm(f"Create a scheduled task?\n  task: {task}\n  when: {describe(rule)}\n  workspace: "
                             f"{agent.workspace}\nIt runs while the omni-agent app is open, with the permission "
                             "mode set at that time."):
            return "User declined creating this schedule.", True
        try:
            it = agent.schedules.add(task, when, str(agent.workspace))
        except ValueError as e:
            return str(e), True
        agent.emit("schedules_changed")
        return (f"Scheduled [{it['id']}] {it['when']}: {it['task']}\nNext run: {it['next_run'].replace('T', ' ')}. "
                "It runs while the omni-agent app (app.py) is open."), False

    def list_schedules():
        items = agent.schedules.load()
        if not items:
            return "No scheduled tasks.", False
        return "\n".join(f"[{it['id']}] {it['when']} {'' if it['enabled'] else '(paused) '}next: "
                         f"{(it['next_run'] or '-').replace('T', ' ')} | {it['task']}" for it in items), False

    def cancel_schedule(id: str):
        ok = agent.schedules.remove(id)
        agent.emit("schedules_changed")
        return (f"Removed schedule {id}.", False) if ok else (f"No schedule with id {id}.", True)

    T = lambda *a, **k: reg.add(Tool(*a, toolset="schedule", **k))
    T("schedule_task", "Schedule a task to run later or repeatedly. task: the full instruction, as if the user "
      "typed it then. when: 'every 30m', 'daily 09:00', 'weekdays 18:30', 'mon,fri 07:00', 'once 2026-11-20 14:00', "
      "'in 20m'. The user approves it.", {"task": {"type": "string"}, "when": {"type": "string"}}, ["task", "when"],
      schedule_task)
    T("list_schedules", "List scheduled tasks.", {}, [], list_schedules)
    T("cancel_schedule", "Delete a scheduled task by id.", {"id": {"type": "string"}}, ["id"], cancel_schedule)
