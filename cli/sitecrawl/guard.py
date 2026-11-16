"""Security guards used by the crawler.

* URL validation + SSRF protection (blocks private / loopback / link-local /
  metadata addresses unless the operator explicitly allows private hosts).
* Prompt-injection scanner: website text is untrusted data. Anything that looks
  like an instruction aimed at an AI system is flagged and *quarantined* so it
  never reaches the summary or a downstream LLM.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "instance-data"}


class GuardError(Exception):
    """Raised when a URL violates the security policy."""


class UnreachableError(GuardError):
    """Host could not be resolved - not a policy violation, the site is unreachable."""


def validate_url(url: str, allow_private: bool = False) -> str:
    """Return a normalised URL or raise GuardError."""
    if not isinstance(url, str) or len(url) > 2048:
        raise GuardError("URL missing or too long")
    if any(c in url for c in "\r\n\t\x00 "):
        raise GuardError("URL contains whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise GuardError(f"scheme '{parts.scheme}' not allowed (only http/https)")
    if not parts.hostname:
        raise GuardError("URL has no host")
    if parts.username or parts.password:
        raise GuardError("credentials in URL are not allowed")
    host = parts.hostname.lower()
    if host in BLOCKED_HOSTNAMES:
        raise GuardError(f"host '{host}' is blocked")
    if parts.port is not None and parts.port not in (80, 443) and not allow_private:
        # unusual ports are a common SSRF vector; allowed only in local/dev mode
        raise GuardError(f"port {parts.port} not allowed")
    check_host_ip(host, allow_private)
    return parts._replace(fragment="").geturl()


def check_host_ip(host: str, allow_private: bool) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnreachableError(f"DNS lookup failed for '{host}' ({exc.strerror})") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            # link-local covers 169.254.169.254 cloud metadata -> never allowed
            raise GuardError(f"host '{host}' resolves to blocked address {ip}")
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_reserved):
            raise GuardError(f"host '{host}' resolves to private address {ip}")


# --------------------------------------------------------------------------
# Prompt-injection scanner
# --------------------------------------------------------------------------
INJECTION_PATTERNS: list[tuple[str, str]] = [
    ("override_instructions", r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your|system)\b[^.\n]{0,30}\b(instructions?|prompts?|rules|directives)\b"),
    ("role_hijack", r"\byou are (now|no longer)\b|\bact as (an?|the)\b[^.\n]{0,30}\b(ai|assistant|model|system)\b|\bnew (system )?persona\b"),
    ("system_prompt_probe", r"\b(reveal|print|show|leak|output)\b[^.\n]{0,30}\b(system prompt|hidden instructions|api key|secret|environment variables?)\b"),
    ("command_execution", r"\b(run|execute)\b[^.\n]{0,20}\b(command|shell|bash|powershell|script)\b|\brm\s+-rf\b|\bcurl\s+https?://\S+\s*\|\s*(sh|bash)\b"),
    ("chat_template_tokens", r"<\|im_start\|>|<\|im_end\|>|\[INST\]|<<SYS>>|^\s*(system|assistant)\s*:"),
    ("ai_directed_note", r"\b(note|message|instruction)s? (to|for) (the )?(ai|llm|assistant|chatbot|language model)\b|\bif you are an? (ai|llm|language model)\b"),
    ("exfiltration", r"\b(send|post|upload|email)\b[^.\n]{0,40}\b(conversation|chat history|credentials|cookies|tokens?)\b"),
]
_COMPILED = [(name, re.compile(p, re.IGNORECASE | re.MULTILINE)) for name, p in INJECTION_PATTERNS]


def scan_text(text: str) -> list[dict]:
    findings = []
    for name, rx in _COMPILED:
        for m in rx.finditer(text):
            start = max(0, m.start() - 60)
            end = min(len(text), m.end() + 60)
            snippet = " ".join(text[start:end].split())
            findings.append({"rule": name, "match": m.group(0)[:120], "snippet": snippet[:240]})
            if len(findings) > 20:
                return findings
    return findings


def is_suspicious_sentence(sentence: str) -> bool:
    return any(rx.search(sentence) for _, rx in _COMPILED)
