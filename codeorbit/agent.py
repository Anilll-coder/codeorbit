"""A fully local agent: an Ollama model driving the CodeOrbit MCP tools.

`codeorbit ask` retrieves once and answers once. An agent decides for itself
what to look up, reads the result, and looks up more - which is what a question
like "is this function safe to change" actually needs, because the answer
depends on what the first lookup turns up.

It speaks real MCP over stdio to the same server Claude Code or Cursor would
connect to. That is deliberate: it exercises the actual protocol rather than a
shortcut through Python imports, so if this works, the server works.

The hard constraint is that tool calling is a property of the model's chat
TEMPLATE, not of Ollama. A model without it does not fail loudly - it invents a
plausible-looking result and states it as fact. phi4-mini does exactly that:
asked to look a symbol up, it replies with fabricated JSON describing a symbol
it never fetched. So support is probed before the loop starts, and a model that
cannot call tools is refused rather than trusted.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .llm import OLLAMA_URL, OllamaError

# Small models that DO carry a tool-calling template, cheapest first. Checked
# against what the user already has before anything is suggested.
TOOL_CAPABLE_HINTS = [
    ("llama3.2:3b", "~2.0 GB, the best reasoning that reliably calls tools at this size"),
    ("qwen2.5:3b", "~1.9 GB"),
    ("qwen2.5-coder:1.5b", "~1.0 GB, code-tuned"),
    ("qwen2.5:0.5b", "~0.4 GB, very weak but does call tools"),
]

MAX_ROUNDS = 6          # a wrong loop costs minutes on CPU; cap it hard

# Context budget. On a CPU box every token costs wall-clock time twice: once to
# ingest and again because a longer prompt slows generation. A tool that returns
# the whole project overview is useful once and dead weight for the rest of the
# session, so results are clipped going in and stale ones are dropped between
# questions rather than accumulating.
MAX_TOOL_CHARS = 2200       # per tool result fed back to the model
MAX_CONTEXT_CHARS = 14000   # total across the message list, before pruning
HISTORY_TURNS = 3           # prior question/answer pairs kept in a session
HISTORY_ANSWER_CHARS = 400  # how much of each prior answer is worth keeping

SYSTEM = (
    "You are a code analysis assistant with tools that read a knowledge graph of "
    "the user's repository.\n"
    "Rules:\n"
    "1. ALWAYS use the tools to look things up. Never answer from memory or "
    "invent a function, file or line number.\n"
    "2. Start with codeorbit_explore for any question about how the code works.\n"
    "3. The tools return real source. Treat it as read.\n"
    "4. When you have enough, answer in plain prose citing symbols and file:line.\n"
    "5. If the tools return nothing useful, say so rather than guessing.\n"
    "6. BE BRIEF. At most 6 sentences, or 5 short bullets. A tool may hand you "
    "fifty symbols; name the two or three that matter and say why, rather than "
    "listing them all back. The user can ask for more."
)


@dataclass
class Step:
    tool: str
    args: dict
    result: str = ""


@dataclass
class Run:
    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    rounds: int = 0
    stopped: str = ""        # why the loop ended, if not by answering
    truncated: bool = False  # the model hit its token budget mid-sentence


def supports_tools(model: str, timeout: int = 180) -> tuple[bool, str]:
    """Probe whether `model` actually emits tool_calls.

    Cheap and decisive: one trivial tool the model has every reason to call. A
    model that answers in prose, or fabricates a result, has no tool template.
    """
    probe = [{
        "type": "function",
        "function": {
            "name": "ping",
            "description": "Return the current status. Call this to check status.",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=timeout, json={
            "model": model,
            "messages": [{"role": "user", "content": "Check the status using your tool."}],
            "tools": probe,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 64},
        })
        r.raise_for_status()
    except requests.RequestException as e:
        return False, f"Ollama request failed: {e}"

    msg = r.json().get("message", {})
    if msg.get("tool_calls"):
        return True, ""
    return False, (msg.get("content") or "")[:200]


def installed_models() -> list[str]:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def suggest_model() -> str:
    """A message naming a tool-capable model, preferring one already present."""
    have = {m.split(":")[0]: m for m in installed_models()}
    lines = []
    for name, note in TOOL_CAPABLE_HINTS:
        base = name.split(":")[0]
        if base in have:
            lines.append(f"  {have[base]}  (already installed) - {note}")
        else:
            lines.append(f"  ollama pull {name}   # {note}")
    return "\n".join(lines)


def mcp_tools_to_ollama(tools) -> list[dict]:
    """MCP tool definitions -> the shape Ollama's chat API expects."""
    out = []
    for t in tools:
        schema = t.input_schema or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "")[:1024],
                "parameters": schema,
            },
        })
    return out


def _chat(model: str, messages: list[dict], tools: list[dict],
          num_ctx: int, num_predict: int, timeout: int = 900) -> dict:
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=timeout, json={
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": num_ctx,
                    "num_predict": num_predict},
    })
    if r.status_code >= 400:
        raise OllamaError(f"Ollama returned {r.status_code}: {r.text[:300]}")
    data = r.json()
    msg = data.get("message", {})
    # done_reason "length" means it ran out of budget, not that it finished.
    # Without this a severed answer is indistinguishable from a complete one,
    # which is how an answer ends mid-word with nothing to say it was cut.
    msg["_truncated"] = data.get("done_reason") == "length"
    return msg


@asynccontextmanager
async def session_for(root: Path, quiet: bool = True):
    """One MCP session, yielded as (session, tools).

    Split out from `run` so an interactive session can hold a single server
    open across many questions. Spawning the server per question meant paying
    a process start and an MCP handshake before every answer, which on the
    machines this targets is most of the time-to-first-token.

    `quiet` sends the server's stderr to nowhere. That stream is diagnostics
    meant for the agent that launched the server, and in an interactive session
    it lands in the middle of the user's prompt line.
    """
    import contextlib
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codeorbit.cli", "mcp", "--path", str(root)],
    )

    with contextlib.ExitStack() as stack:
        errlog = sys.stderr
        if quiet:
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                yield session, mcp_tools_to_ollama(listed.tools)


def prune(messages: list[dict], budget: int = MAX_CONTEXT_CHARS) -> list[dict]:
    """Drop the oldest tool results until the message list fits the budget.

    Tool results are what actually grows: a single overview can be two
    thousand characters and there may be six of them. The system prompt and the
    question are never dropped, and neither is the most recent tool result,
    which is usually the one being reasoned about.
    """
    def size(ms):
        return sum(len(str(m.get("content") or "")) for m in ms)

    if size(messages) <= budget:
        return messages

    out = list(messages)
    # Indices of tool results, oldest first, excluding the last one.
    tool_at = [i for i, m in enumerate(out) if m.get("role") == "tool"]
    for i in tool_at[:-1]:
        if size(out) <= budget:
            break
        out[i] = dict(out[i], content="[earlier tool result dropped to save context]")
    return out


async def ask_once(session, tools, question: str, model: str, *,
                   history: list[dict] | None = None,
                   max_rounds: int = MAX_ROUNDS, num_ctx: int = 8192,
                   num_predict: int = 700, on_step=None) -> Run:
    """Answer one question on an already-open session."""
    out = Run()
    messages = [{"role": "system", "content": SYSTEM}]
    messages += history or []
    messages.append({"role": "user", "content": question})

    for rnd in range(1, max_rounds + 1):
        out.rounds = rnd
        msg = _chat(model, prune(messages), tools, num_ctx, num_predict)
        calls = msg.get("tool_calls") or []

        if not calls:
            out.answer = (msg.get("content") or "").strip()
            out.truncated = bool(msg.get("_truncated"))
            if not out.answer:
                out.stopped = "the model returned nothing"
            return out

        messages.append({
            "role": "assistant",
            "content": msg.get("content", ""),
            "tool_calls": calls,
        })

        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            step = Step(tool=name, args=args)
            try:
                res = await session.call_tool(name, args)
                step.result = "\n".join(
                    c.text for c in res.content if hasattr(c, "text"))
            except Exception as e:
                # Feed the failure back as a tool result rather than aborting:
                # the model can pick a different tool.
                step.result = f"That tool call failed: {e}"

            out.steps.append(step)
            if on_step:
                on_step(step)

            messages.append({
                "role": "tool",
                "content": step.result[:MAX_TOOL_CHARS],
                "tool_name": name,
            })

    out.stopped = (
        f"stopped after {max_rounds} rounds without a final answer - "
        "the model kept calling tools"
    )
    return out


def remember(history: list[dict], question: str, answer: str) -> list[dict]:
    """Carry a short question/answer memory into the next turn.

    Only the prose, never the tool results: those are large, specific to the
    question that fetched them, and stale by the next one. Keeping them was the
    difference between a session that stays responsive and one that slows down
    with every question asked.
    """
    history = history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer[:HISTORY_ANSWER_CHARS]},
    ]
    return history[-HISTORY_TURNS * 2:]


async def run(root: Path, question: str, model: str, *,
              max_rounds: int = MAX_ROUNDS, num_ctx: int = 8192,
              num_predict: int = 700, on_step=None) -> Run:
    """Drive the MCP tools with `model` until it answers one question.

    Connects as a real MCP client over stdio, exactly as an external agent
    would.
    """
    async with session_for(root) as (session, tools):
        return await ask_once(session, tools, question, model,
                              max_rounds=max_rounds, num_ctx=num_ctx,
                              num_predict=num_predict, on_step=on_step)
