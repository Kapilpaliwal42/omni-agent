"""Tool registry with toolsets.

A 9B model gets worse as its tool list grows, so tools live in named toolsets. Only the active
toolsets are sent to the model; `use_toolset` (or keyword routing at task start) switches others on.

Confirmation levels:
  never   read-only, never asks
  edit    asks in `ask` mode (file writes)
  act     asks in `ask` and `auto` mode, free in `yolo` (desktop control, launching programs)
  always  asks in every mode, yolo included (self-changes, installing skills or MCP servers)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

CONFIRM_LEVELS = ("never", "edit", "act", "always")


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    required: list[str]
    fn: Callable[..., tuple[str, bool]]
    toolset: str = "core"
    confirm: str = "never"
    # Optional per-call check: returns a reason string when this particular call needs confirmation
    # (e.g. a click on a "Buy now" button), or None. Applies in ask and auto mode.
    guard: Callable[[dict], str | None] | None = None

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": self.required}}}


@dataclass
class Toolset:
    name: str
    description: str
    default_on: bool = False
    keywords: str = ""          # regex; a task matching it switches the toolset on at the start


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)
    toolsets: dict[str, Toolset] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)

    def add_toolset(self, name: str, description: str, default_on: bool = False, keywords: str = ""):
        self.toolsets[name] = Toolset(name, description, default_on, keywords)
        if default_on:
            self.active.add(name)

    def add(self, tool: Tool):
        if tool.confirm not in CONFIRM_LEVELS:
            raise ValueError(f"bad confirm level {tool.confirm!r} for {tool.name}")
        if tool.toolset not in self.toolsets:
            self.add_toolset(tool.toolset, tool.toolset)
        self.tools[tool.name] = tool

    def remove_toolset(self, name: str):
        for t in [t for t in self.tools.values() if t.toolset == name]:
            del self.tools[t.name]
        self.toolsets.pop(name, None)
        self.active.discard(name)

    def active_tools(self) -> list[Tool]:
        return [t for t in self.tools.values() if t.toolset in self.active]

    def active_names(self) -> set[str]:
        return {t.name for t in self.active_tools()}

    def inactive_toolsets(self) -> list[Toolset]:
        return [s for s in self.toolsets.values()
                if s.name not in self.active and any(t.toolset == s.name for t in self.tools.values())]

    def reset_active(self):
        self.active = {s.name for s in self.toolsets.values() if s.default_on}
