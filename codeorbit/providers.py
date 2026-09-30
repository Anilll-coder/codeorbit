"""Pluggable chat backends for `codeorbit agent`.

`codeorbit agent` drives the same MCP tools any external agent would - it is a
real MCP client, talking the real protocol, to the same server Claude Code or
Cursor connects to (see `agent.py`). What it did NOT generalise is the thing
deciding which tool to call next: that was always Ollama, reached over its
native `/api/chat`, and in practice restricted to a handful of small models
whose chat template happens to carry tool-calling support at all.

That made "any agent, through MCP" true of the server and false of the one
agent this project ships. This module is the fix. One small shape -

    chat(model, messages, tools, num_ctx, num_predict) -> {
        "content": str, "tool_calls": [{"function": {"name", "arguments"}}],
        "_truncated": bool,
    }

- is all `agent.ask_once`'s loop actually depends on, and it is exactly what
`agent._chat` (the original, untouched, still-default Ollama path) already
returns. This module supplies the same shape for two more backends:

  openai      anything that speaks the OpenAI chat-completions wire format:
              LM Studio, vLLM, llama.cpp's own server, text-generation-webui,
              Ollama's OpenAI-compatible endpoint, OpenRouter, Groq, or the
              real OpenAI API. One implementation covers all of them because
              they agree on the request and response shape; only the base URL
              and (optionally) an API key differ.
  anthropic   Claude models directly, over the Messages API. Its wire format
              is the most different of the three - content blocks, no "tool"
              role, the system prompt as a top-level field rather than a
              message - so this is where the translation actually has to
              earn its keep.

`agent.py` itself is unmodified for the Ollama path: every existing test that
pins its behaviour keeps pinning the same code. The loop grows exactly one new
knob, `chat_fn`, defaulting to `None` (meaning "use the built-in Ollama path,
as before"). A provider here is nothing more than a `chat_fn` plus a matching
`supports_tools` and `list_models`.

Credentials are read from the environment ONLY, never accepted as CLI text -
the same rule this project already holds for every other secret. A local
OpenAI-compatible server that needs no key (LM Studio, a bare llama.cpp
server) is a first-class case: no key set means no Authorization header sent,
not a refusal to start.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import requests

DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# What `codeorbit agent --provider X` accepts, and the env var each reads its
# key from by default. "ollama" is not listed: it is the original path in
# agent.py / llm.py and needs no entry here at all.
PROVIDER_IDS = ("ollama", "openai", "anthropic")

DEFAULT_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

# A short, honest fallback when a provider's own model listing fails or is not
# offered (Anthropic's API key scopes often cannot list models). These are
# suggestions to print, never silently substituted for what the user asked for.
MODEL_HINTS = {
    "openai": [
        ("gpt-4o-mini", "cheap, fast, reliable tool use"),
        ("gpt-4o", "stronger reasoning, costs more"),
    ],
    "anthropic": [
        ("claude-sonnet-5", "the model this project is named for calling"),
        ("claude-haiku-4-5", "cheaper and faster, still calls tools well"),
    ],
}


class ProviderError(RuntimeError):
    """A backend-agnostic failure. `codeorbit agent` catches this the same
    way it already catches `llm.OllamaError` - as a normal, reported failure,
    never a stack trace in the user's face."""


@dataclass(frozen=True)
class Provider:
    """Everything `codeorbit agent` needs from a backend, as plain functions.

    A dataclass of callables rather than a class hierarchy: there are three
    backends and they do not share behaviour beyond this shape, so a common
    base class would exist only to be overridden every time. This is the
    thinnest thing that lets `cli.py` treat all three identically.
    """
    id: str
    base_url: str
    api_key: str | None

    def chat(self, model: str, messages: list[dict], tools: list[dict],
             num_ctx: int, num_predict: int, timeout: int = 900) -> dict:
        if self.id == "openai":
            return _openai_chat(self.base_url, self.api_key, model, messages,
                                tools, num_ctx, num_predict, timeout)
        return _anthropic_chat(self.base_url, self.api_key, model, messages,
                               tools, num_predict, timeout)

    def supports_tools(self, model: str, timeout: int = 180) -> tuple[bool, str]:
        if self.id == "openai":
            return _openai_supports_tools(self.base_url, self.api_key, model, timeout)
        return _anthropic_supports_tools(self.base_url, self.api_key, model, timeout)

    def list_models(self) -> list[str]:
        if self.id == "openai":
            return _openai_list_models(self.base_url, self.api_key)
        return _anthropic_list_models(self.base_url, self.api_key)

    def model_hints(self) -> list[tuple[str, str]]:
        return MODEL_HINTS.get(self.id, [])


def resolve(provider_id: str, base_url: str | None = None,
           api_key_env: str | None = None) -> Provider:
    """Build a `Provider` from CLI flags and the environment.

    `provider_id == "ollama"` is rejected here on purpose: that path is
    `agent.py`'s own functions, called directly, with no indirection through
    this module at all. Routing it through here too would mean two places
    could disagree about what "ollama" means.
    """
    if provider_id not in ("openai", "anthropic"):
        raise ValueError(f"providers.resolve does not handle {provider_id!r}")

    default_base = DEFAULT_OPENAI_BASE if provider_id == "openai" else DEFAULT_ANTHROPIC_BASE
    url = (base_url or os.environ.get(f"{provider_id.upper()}_BASE_URL")
           or default_base).rstrip("/")

    env_name = api_key_env or DEFAULT_KEY_ENV[provider_id]
    key = os.environ.get(env_name) or None
    return Provider(id=provider_id, base_url=url, api_key=key)


def key_missing_hint(provider_id: str, api_key_env: str | None = None) -> str | None:
    """None if a key is set or not required; otherwise what to set and how.

    Only `anthropic` treats a missing key as fatal. `openai` does not: the
    same code path serves a local server (LM Studio, llama.cpp) that takes no
    key at all, and refusing to start on principle there would break the
    no-keys, local-first case this project cares most about.
    """
    if provider_id != "anthropic":
        return None
    env_name = api_key_env or DEFAULT_KEY_ENV[provider_id]
    if os.environ.get(env_name):
        return None
    return (f"{env_name} is not set. Anthropic's API requires a key:\n"
            f"  export {env_name}=sk-ant-...\n"
            "Get one at https://console.anthropic.com/settings/keys")


# ---------------------------------------------------------------- shared

def _headers(api_key: str | None, *, bearer: bool, anthropic: bool = False) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key and anthropic:
        h["x-api-key"] = api_key
        h["anthropic-version"] = ANTHROPIC_VERSION
    elif api_key and bearer:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _request_error(e: Exception, base_url: str) -> ProviderError:
    return ProviderError(f"Request to {base_url} failed: {e}")


# Every `tool` message that immediately follows an assistant turn with N
# tool_calls corresponds, in order, to one of those N calls - `ask_once`
# appends exactly one `tool` message per call, in call order, before the next
# assistant or user turn. Neither Ollama's shape nor this project's own
# message-building carries an id through that round trip, so both the OpenAI
# and Anthropic translators need to re-pair tool results to their calls by
# position. This is the one piece of bookkeeping they share.
def _pair_tool_calls(messages: list[dict]) -> list[tuple[int, int, dict] | None]:
    """Parallel to `messages`: for a `tool` message, (assistant_idx, call_idx,
    call); else None. Synthetic ids are assigned as `tc_{assistant_idx}_{i}`."""
    out: list = [None] * len(messages)
    pending: list[tuple[int, int, dict]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            pending = [(i, j, c) for j, c in enumerate(m["tool_calls"])]
        elif role == "tool":
            if pending:
                out[i] = pending.pop(0)
        else:
            pending = []
    return out


# ------------------------------------------------------------ openai-compat

def _openai_messages(messages: list[dict], pairs) -> list[dict]:
    out = []
    for m, pair in zip(messages, pairs):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for j, call in enumerate(m["tool_calls"]):
                fn = call.get("function", {})
                calls.append({
                    "id": f"tc_{len(out)}_{j}",
                    "type": "function",
                    "function": {"name": fn.get("name", ""),
                                "arguments": json.dumps(fn.get("arguments") or {})},
                })
            out.append({"role": "assistant", "content": m.get("content") or None,
                        "tool_calls": calls})
        elif role == "tool":
            call_id = f"tc_{pair[0]}_{pair[1]}" if pair else "tc_unknown"
            out.append({"role": "tool", "tool_call_id": call_id,
                        "content": m.get("content", "")})
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def _openai_chat(base_url: str, api_key: str | None, model: str,
                 messages: list[dict], tools: list[dict], num_ctx: int,
                 num_predict: int, timeout: int) -> dict:
    pairs = _pair_tool_calls(messages)
    body = {
        "model": model,
        "messages": _openai_messages(messages, pairs),
        "tools": tools,
        "temperature": 0.1,
        "max_tokens": num_predict,
    }
    try:
        r = requests.post(f"{base_url}/chat/completions",
                          headers=_headers(api_key, bearer=True),
                          json=body, timeout=timeout)
    except requests.RequestException as e:
        raise _request_error(e, base_url) from e
    if r.status_code >= 400:
        raise ProviderError(f"{base_url} returned {r.status_code}: {r.text[:300]}")

    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function", {})
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {}
        calls.append({"function": {"name": fn.get("name", ""), "arguments": args or {}}})

    return {
        "content": (msg.get("content") or "").strip(),
        "tool_calls": calls,
        "_truncated": choice.get("finish_reason") == "length",
    }


def _openai_supports_tools(base_url: str, api_key: str | None, model: str,
                           timeout: int) -> tuple[bool, str]:
    probe = [{
        "type": "function",
        "function": {
            "name": "ping",
            "description": "Return the current status. Call this to check status.",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    try:
        r = requests.post(f"{base_url}/chat/completions",
                          headers=_headers(api_key, bearer=True), timeout=timeout,
                          json={
                              "model": model,
                              "messages": [{"role": "user",
                                           "content": "Check the status using your tool."}],
                              "tools": probe, "temperature": 0, "max_tokens": 64,
                          })
    except requests.RequestException as e:
        return False, f"Request failed: {e}"
    if r.status_code >= 400:
        return False, f"{base_url} returned {r.status_code}: {r.text[:200]}"

    msg = (r.json().get("choices") or [{}])[0].get("message", {})
    if msg.get("tool_calls"):
        return True, ""
    return False, (msg.get("content") or "")[:200]


def _openai_list_models(base_url: str, api_key: str | None) -> list[str]:
    try:
        r = requests.get(f"{base_url}/models",
                        headers=_headers(api_key, bearer=True), timeout=10)
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []) if "id" in m)
    except Exception:      # noqa: BLE001 - listing is a convenience, not a need
        return []


# -------------------------------------------------------------- anthropic

def _anthropic_messages(messages: list[dict], pairs) -> tuple[str, list[dict]]:
    """(system_prompt, messages) - Anthropic takes the system prompt as its
    own top-level field, never as a message in the list."""
    system = ""
    out: list[dict] = []

    for m, pair in zip(messages, pairs):
        role = m.get("role")

        if role == "system":
            system = (system + "\n" + m.get("content", "")).strip()
            continue

        if role == "assistant" and m.get("tool_calls"):
            blocks = []
            text = m.get("content") or ""
            if text:
                blocks.append({"type": "text", "text": text})
            for j, call in enumerate(m["tool_calls"]):
                fn = call.get("function", {})
                blocks.append({
                    "type": "tool_use", "id": f"tc_{len(out)}_{j}",
                    "name": fn.get("name", ""), "input": fn.get("arguments") or {},
                })
            out.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            call_id = f"tc_{pair[0]}_{pair[1]}" if pair else "tc_unknown"
            block = {"type": "tool_result", "tool_use_id": call_id,
                     "content": m.get("content", "")}
            # Anthropic wants every tool_result from one assistant turn bundled
            # into a single following user message, not one each - otherwise
            # it rejects the request as having tool_use blocks with no
            # matching result in the very next turn.
            if out and out[-1]["role"] == "user" and all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        out.append({"role": role, "content": m.get("content", "")})

    return system, out


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    """The OpenAI-shaped tool list `mcp_tools_to_ollama` already produces,
    reshaped to Anthropic's flat `input_schema` form."""
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _anthropic_chat(base_url: str, api_key: str | None, model: str,
                    messages: list[dict], tools: list[dict],
                    num_predict: int, timeout: int) -> dict:
    pairs = _pair_tool_calls(messages)
    system, anth_messages = _anthropic_messages(messages, pairs)
    body = {
        "model": model,
        "system": system,
        "messages": anth_messages,
        "tools": _anthropic_tools(tools),
        "max_tokens": max(num_predict, 1),
        "temperature": 0.1,
    }
    try:
        r = requests.post(f"{base_url}/messages",
                          headers=_headers(api_key, bearer=False, anthropic=True),
                          json=body, timeout=timeout)
    except requests.RequestException as e:
        raise _request_error(e, base_url) from e
    if r.status_code >= 400:
        raise ProviderError(f"{base_url} returned {r.status_code}: {r.text[:300]}")

    data = r.json()
    text_parts = []
    calls = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"function": {"name": block.get("name", ""),
                                       "arguments": block.get("input") or {}}})

    return {
        "content": "".join(text_parts).strip(),
        "tool_calls": calls,
        "_truncated": data.get("stop_reason") == "max_tokens",
    }


def _anthropic_supports_tools(base_url: str, api_key: str | None, model: str,
                              timeout: int) -> tuple[bool, str]:
    probe = [{
        "name": "ping",
        "description": "Return the current status. Call this to check status.",
        "input_schema": {"type": "object", "properties": {}},
    }]
    try:
        r = requests.post(f"{base_url}/messages",
                          headers=_headers(api_key, bearer=False, anthropic=True),
                          timeout=timeout, json={
                              "model": model,
                              "messages": [{"role": "user",
                                           "content": "Check the status using your tool."}],
                              "tools": probe, "max_tokens": 64, "temperature": 0,
                          })
    except requests.RequestException as e:
        return False, f"Request failed: {e}"
    if r.status_code >= 400:
        return False, f"{base_url} returned {r.status_code}: {r.text[:200]}"

    blocks = r.json().get("content") or []
    if any(b.get("type") == "tool_use" for b in blocks):
        return True, ""
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return False, text[:200]


def _anthropic_list_models(base_url: str, api_key: str | None) -> list[str]:
    try:
        r = requests.get(f"{base_url}/models",
                        headers=_headers(api_key, bearer=False, anthropic=True),
                        timeout=10)
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []) if "id" in m)
    except Exception:      # noqa: BLE001 - listing is a convenience, not a need
        return []
