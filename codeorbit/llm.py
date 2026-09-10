"""Ollama client. Local only - nothing leaves this machine."""
from __future__ import annotations

import json
import os
from typing import Iterator

import requests

OLLAMA_URL = "http://127.0.0.1:11434"

# phi4-mini (3.8B, ~2.5GB) is the sweet spot on a 7-8GB CPU-only box: big enough
# to follow a grounded prompt, small enough to leave room for the OS. Anything
# 7B+ swaps here and the answer takes minutes.
DEFAULT_MODEL = "phi4-mini"
EMBED_MODEL = "nomic-embed-text"


class OllamaError(RuntimeError):
    pass


def available() -> bool:
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


def installed() -> bool:
    """Is the ollama binary on PATH at all?"""
    import shutil
    return shutil.which("ollama") is not None


def start_server(timeout: float = 25.0) -> bool:
    """Start `ollama serve` in the background and wait for it to answer.

    Telling someone to open a second terminal and run a daemon before they can
    ask a question is a step that adds nothing: they are going to say yes every
    time. So start it here, detached, so it outlives this command and the next
    one finds it already up.

    Returns False if ollama is not installed or did not come up in time; the
    caller decides what to say about it.
    """
    import subprocess
    import time

    if available():
        return True
    if not installed():
        return False

    kwargs = {"stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    try:
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console window,
            # and it must not die when this terminal closes.
            subprocess.Popen(["ollama", "serve"],
                             creationflags=0x00000008 | 0x00000200, **kwargs)
        else:
            subprocess.Popen(["ollama", "serve"], start_new_session=True, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if available():
            return True
        time.sleep(0.4)
    return False


NOT_INSTALLED = (
    "Ollama is not installed, and the agent needs a local model to drive.\n"
    "  Install it from https://ollama.com, then: ollama pull llama3.2:3b"
)

WOULD_NOT_START = (
    "Ollama is installed but did not start.\n"
    "  Try it by hand to see why:  ollama serve"
)


def models() -> list[str]:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def generate(prompt: str, model: str = DEFAULT_MODEL, system: str | None = None,
             temperature: float = 0.1, num_ctx: int = 8192,
             num_predict: int = 320) -> str:
    """One-shot completion. Low temperature: this is retrieval-grounded Q&A,
    not creative writing - we want it to quote the context, not embroider it.

    num_predict is capped because on CPU this machine generates ~1-2 tokens/sec,
    so total time is dominated by how much the model says, not by how much
    context it was given. Capping output is the single biggest speed lever.
    """
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if system:
        body["system"] = system
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=body, timeout=600)
        r.raise_for_status()
    except requests.RequestException as e:
        raise OllamaError(
            f"Ollama request failed: {e}\nIs it running? Try: ollama serve"
        ) from e
    return r.json().get("response", "").strip()


def stream(prompt: str, model: str = DEFAULT_MODEL, system: str | None = None,
           temperature: float = 0.1, num_ctx: int = 8192,
           num_predict: int = 320) -> Iterator[str]:
    """Token stream, so a slow CPU model still feels alive."""
    body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if system:
        body["system"] = system
    try:
        with requests.post(f"{OLLAMA_URL}/api/generate", json=body,
                           stream=True, timeout=600) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = chunk.get("response")
                if piece:
                    yield piece
                if chunk.get("done"):
                    return
    except requests.RequestException as e:
        raise OllamaError(
            f"Ollama request failed: {e}\nIs it running? Try: ollama serve"
        ) from e


def embed(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    """Embeddings for semantic symbol search (nomic-embed-text is already local)."""
    out = []
    for t in texts:
        r = requests.post(f"{OLLAMA_URL}/api/embeddings",
                          json={"model": model, "prompt": t}, timeout=120)
        r.raise_for_status()
        out.append(r.json()["embedding"])
    return out
