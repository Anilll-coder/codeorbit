"""Ollama client. Local only - nothing leaves this machine."""
from __future__ import annotations

import json
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
