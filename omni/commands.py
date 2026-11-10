"""Slash commands, shared by the terminal CLI and the app."""
from __future__ import annotations

from .util import STATE

COMMANDS = {
    "/improve": "change the agent's own code: /improve <what>",
    "/learn": "turn the last task into a reusable skill: /learn [focus]",
    "/feedback": "teach it how you want this kind of task done: /feedback <what to do differently>",
    "/redo": "redo the last task, optionally with feedback: /redo [what to do differently]",
    "/schedules": "list scheduled tasks",
    "/allowed": "show or clear 'always allow' answers: /allowed [clear]",
    "/skills": "list learned skills",
    "/mcp": "list MCP servers",
    "/memory": "show remembered facts",
    "/history": "self-update and skill commits",
    "/rollback": "revert the last self-update or skill commit",
    "/reset": "clear the conversation",
    "/help": "list commands",
}

LEARN_PROMPT = """Earlier in this session you did this work:
{history}

Turn the most reusable part of it into a skill with create_skill, so next time a similar task goes faster
and more reliably. {hint}
- instructions: the concrete steps that worked, the sites/apps/commands/paths used, and the mistakes to avoid.
- Only add tool_code if a small Python function would clearly help; if you do, test_code is required.
- keywords: a short regex of words a future task would contain.
Then reply with one line naming the skill."""


def is_command(text: str) -> bool:
    return text.split(" ", 1)[0] in COMMANDS


def run_command(agent, text: str) -> str:
    cmd, _, rest = text.strip().partition(" ")
    rest = rest.strip()
    if cmd == "/help":
        return "\n".join(f"{k:10} {v}" for k, v in COMMANDS.items())
    if cmd == "/reset":
        agent.messages.clear()
        agent.history.clear()
        return "Conversation cleared."
    if cmd == "/improve":
        if not rest:
            return "usage: /improve <what to change in the agent>"
        if not hasattr(agent, "self_improve"):
            return "Self-update is turned off (--no-self-update)."
        return agent.self_improve(rest)[0]
    if cmd == "/learn":
        if not agent.history:
            return "Nothing to learn from yet: do a task first."
        agent.registry.reset_active()
        msgs = [{"role": "user", "content": LEARN_PROMPT.format(
            history="\n".join(agent.history[-3:]), hint=f"Focus: {rest}" if rest else "")}]
        return agent.loop(msgs, 8, True)[0]
    if cmd in ("/feedback", "/redo"):
        last = getattr(agent, "last_task", None)
        rule = ""
        if rest:
            if not hasattr(agent, "learn_feedback"):
                return "The memory module is off, so feedback can't be saved."
            rule = agent.learn_feedback(rest, last["task"] if last else "")
        if cmd == "/feedback":
            return f"Saved. From now on: {rule}" if rule else "usage: /feedback <what to do differently next time>"
        if not last:
            return "Nothing to redo yet: do a task first."
        retry = last["task"] + (f"\n\nYour previous attempt was not what I wanted. My feedback: {rest}" if rest else "")
        agent.run(retry)
        return f"Saved preference: {rule}" if rule else ""
    if cmd == "/schedules":
        if not hasattr(agent, "schedules"):
            return "Scheduling is off."
        return agent.registry.tools["list_schedules"].fn()[0]
    if cmd == "/allowed":
        if rest == "clear":
            agent.session_allow.clear()
            agent.emit("allow_list", keys=[])
            return "Cleared. Everything asks again."
        return ("Always allowed this session:\n" + "\n".join(f"- {k}" for k in sorted(agent.session_allow))
                if agent.session_allow else "Nothing is on 'always allow'. (/allowed clear resets it.)")
    if cmd == "/skills":
        return agent.skills_index() or "No skills yet."
    if cmd == "/mcp":
        return agent.registry.tools["list_mcp_servers"].fn()[0]
    if cmd == "/memory":
        p = STATE / "memory.md"
        return p.read_text(encoding="utf-8") if p.is_file() else "(nothing remembered)"
    if cmd == "/history":
        from .selfupdate import history
        return history()
    if cmd == "/rollback":
        from .selfupdate import rollback
        return rollback(agent)
    return f"Unknown command {cmd}. /help lists them."
