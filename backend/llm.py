"""Tiny client for any OpenAI-compatible chat-completions endpoint.

The LLM is OPTIONAL. ProofCrawl works fully offline with deterministic
fallbacks; when an LLM is configured it is used for (a) intent parsing and
(b) a grounded abstractive summary. The LLM never produces shell commands:
it can only fill a validated JSON schema.
"""
from __future__ import annotations

import json
import re

import requests

from . import config


class LLMError(Exception):
    pass


def chat_json(system: str, user: str, timeout: float = 25.0, max_tokens: int = 600) -> dict:
    if not config.LLM_ENABLED:
        raise LLMError("LLM not configured")
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    body = {
        "model": config.LLM_MODEL,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    try:
        r = requests.post(f"{config.LLM_BASE_URL}/chat/completions", headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(f"LLM request failed: {type(exc).__name__}") from None
    if r.status_code != 200:
        raise LLMError(f"LLM HTTP {r.status_code}")  # never echo the response body: it may contain the key
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError):
        raise LLMError("unexpected LLM response shape") from None
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise LLMError("LLM did not return JSON")
    try:
        return json.loads(m.group(0))
    except ValueError:
        raise LLMError("LLM returned invalid JSON") from None
