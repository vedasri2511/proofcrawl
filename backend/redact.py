"""Secret redaction applied to anything shown in chat or written to logs."""
from __future__ import annotations

import os
import re

PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for rx in PATTERNS:
        text = rx.sub("[REDACTED]", text)
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        val = os.environ.get(name)
        if val and len(val) > 6:
            text = text.replace(val, "[REDACTED]")
    return text
