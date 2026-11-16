"""Intent layer: natural language  ->  validated TaskSpec.

Two interpreters share one output schema:
  * a deterministic rule-based parser (always available, no API key needed)
  * an optional LLM parser (used when configured; its output is validated and
    falls back to the rules on any problem)

Neither interpreter can produce a command line. They only select an *approved*
task type and fill typed, bounded parameters. The URL must literally appear in
the user's message, so a model cannot invent a target.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import config
from .llm import LLMError, chat_json

Output = Literal["summary", "json", "contacts", "links", "headings", "broken_links", "pages", "security"]
ALL_OUTPUTS = ["summary", "json", "contacts", "links", "headings", "broken_links", "pages", "security"]

URL_RX = re.compile(r"\bhttps?://[^\s<>\"'`]+", re.I)
BARE_DOMAIN_RX = re.compile(r"(?<![@\w/.-])((?:[a-z0-9-]+\.)+(?:com|org|net|io|ai|dev|in|edu|gov|co|uk|info|app|tech|me|us|ac\.in|co\.in))(/[^\s<>\"']*)?\b", re.I)
DANGEROUS_RX = re.compile(r"rm\s+-rf|\bsudo\b|\bdelete (all|the|my) files\b|format (c:|the disk)|/etc/passwd|\bshutdown\b|\bmkfs\b|reverse shell|\bnc -e\b|powershell|cmd\.exe|drop table", re.I)
GREETING_RX = re.compile(r"^\s*(hi|hello|hey|help|what can you do|who are you)\b", re.I)


class TaskSpec(BaseModel):
    intent: Literal["crawl_site", "needs_clarification", "unsupported", "help"]
    url: str | None = None
    outputs: list[Output] = Field(default_factory=lambda: ["summary", "json"])
    max_pages: int = config.DEFAULT_PAGES
    max_depth: int = config.DEFAULT_DEPTH
    check_links: bool = False
    reply: str | None = None           # message for clarification / refusal / help
    interpreted_by: Literal["rules", "llm", "rules (llm fallback)"] = "rules"
    reasoning: list[str] = Field(default_factory=list)

    @field_validator("max_pages")
    @classmethod
    def _cap_pages(cls, v):
        return max(1, min(int(v), config.MAX_PAGES_CAP))

    @field_validator("max_depth")
    @classmethod
    def _cap_depth(cls, v):
        return max(0, min(int(v), config.MAX_DEPTH_CAP))


HELP = (
    "I turn a request into a sandboxed **sitecrawl** CLI run and show you the evidence. Try:\n"
    "- `Crawl https://example.com, extract the important information, give me a summary, and return the data in JSON`\n"
    "- `Find contact details on https://python.org (max 5 pages)`\n"
    "- `Check https://example.org for broken links`\n"
    "Limits: up to %d pages, depth %d, http/https only, robots.txt respected." % (config.MAX_PAGES_CAP, config.MAX_DEPTH_CAP)
)


def find_url(message: str) -> str | None:
    m = URL_RX.search(message)
    if m:
        return m.group(0).rstrip(".,;:!?)]}'\"")
    m = BARE_DOMAIN_RX.search(message)
    if m:
        return "https://" + m.group(0).rstrip(".,;:!?)]}'\"")
    return None


def parse_rules(message: str) -> TaskSpec:
    msg = message.strip()
    low = msg.lower()
    why = []
    url = find_url(msg)
    if DANGEROUS_RX.search(msg):
        return TaskSpec(intent="unsupported", reasoning=["matched a disallowed operation pattern"],
                        reply="I can't run that. I only execute the approved **sitecrawl** tool against public http/https websites, "
                              "never arbitrary shell commands.")
    if not url:
        if GREETING_RX.search(msg) or len(msg) < 4:
            return TaskSpec(intent="help", reply=HELP, reasoning=["greeting / help request"])
        if re.search(r"crawl|scrape|website|site|summari[sz]e|extract|links?|contacts?", low):
            return TaskSpec(intent="needs_clarification", reasoning=["crawl-like request without a URL"],
                            reply="Which website should I crawl? Please include the full URL, e.g. `https://example.com`.")
        return TaskSpec(intent="unsupported", reasoning=["no URL and no supported task"],
                        reply="That isn't something my approved tools can do. I can crawl a website and return a summary, "
                              "structured JSON, contacts, links, headings or broken links.\n\n" + HELP)
    why.append(f"target URL found in message: {url}")

    outputs: list[str] = []
    rules = [
        ("summary", r"summar|overview|what (is|does)|tell me about|describe|gist|tl;?dr|important"),
        ("json", r"\bjson\b|structured|machine[- ]readable"),
        ("contacts", r"contact|e-?mail|phone|address|reach them|social"),
        ("links", r"\blinks?\b|urls|sitemap|navigation"),
        ("headings", r"heading|outline|structure of"),
        ("broken_links", r"broken|dead links?|404"),
        ("pages", r"pages? list|list (the|all) pages|which pages"),
        ("security", r"injection|security|safe|malicious"),
    ]
    for name, rx in rules:
        if re.search(rx, low):
            outputs.append(name)
    if re.search(r"extract|information|info|data|details", low) and "json" not in outputs:
        outputs.append("json")
    if not outputs:
        outputs = ["summary", "json"]
        why.append("no explicit output requested -> default to summary + JSON")
    else:
        why.append("requested outputs: " + ", ".join(outputs))

    max_pages = config.DEFAULT_PAGES
    m = re.search(r"(\d{1,3})\s*(?:pages?|urls)", low) or re.search(r"(?:max|up to|limit)\D{0,10}(\d{1,3})", low)
    if m:
        max_pages = int(m.group(1))
        why.append(f"page limit requested: {max_pages} (server cap {config.MAX_PAGES_CAP})")
    if re.search(r"only (the )?(home ?page|this page|first page)|single page|just (the|this) page", low):
        max_pages, why = 1, why + ["single page requested"]
    max_depth = config.DEFAULT_DEPTH
    m = re.search(r"depth\D{0,6}(\d)", low)
    if m:
        max_depth = int(m.group(1))
    if max_pages == 1:
        max_depth = 0
    return TaskSpec(intent="crawl_site", url=url, outputs=outputs, max_pages=max_pages, max_depth=max_depth,
                    check_links="broken_links" in outputs, reasoning=why)


LLM_SYSTEM = """You convert a user's chat message into a JSON task specification for a website-crawling tool.
Return ONLY a JSON object with keys:
 intent: one of "crawl_site", "needs_clarification", "unsupported", "help"
 url: the exact http(s) URL from the message, or null
 outputs: array subset of ["summary","json","contacts","links","headings","broken_links","pages","security"]
 max_pages: integer 1-25, max_depth: integer 0-3
 reply: short text for the user if intent is not crawl_site, else null
 reasoning: array of 1-3 short strings
Rules: never invent a URL. Never output shell commands. Requests unrelated to crawling a public website are "unsupported"."""


def parse(message: str) -> TaskSpec:
    rule_spec = parse_rules(message)
    if not config.LLM_ENABLED or rule_spec.intent == "unsupported" and DANGEROUS_RX.search(message):
        return rule_spec
    try:
        raw = chat_json(LLM_SYSTEM, message[:2000])
        spec = TaskSpec(
            intent=raw.get("intent", "unsupported"),
            url=raw.get("url"),
            outputs=[o for o in raw.get("outputs") or [] if o in ALL_OUTPUTS] or ["summary", "json"],
            max_pages=raw.get("max_pages") or config.DEFAULT_PAGES,
            max_depth=raw.get("max_depth") if raw.get("max_depth") is not None else config.DEFAULT_DEPTH,
            reply=raw.get("reply"),
            reasoning=[str(r)[:160] for r in (raw.get("reasoning") or [])][:3],
            interpreted_by="llm",
        )
        if spec.intent == "crawl_site":
            # anti-hallucination: the URL must be present in what the user actually typed
            if not spec.url or (spec.url.rstrip("/") not in message and spec.url.split("://", 1)[-1].rstrip("/") not in message):
                raise LLMError("LLM URL not present in user message")
            spec.check_links = "broken_links" in spec.outputs
        elif spec.intent in ("help",) and not spec.reply:
            spec.reply = HELP
        elif not spec.reply:
            spec.reply = rule_spec.reply or HELP
        return spec
    except (LLMError, ValueError, TypeError) as exc:
        rule_spec.interpreted_by = "rules (llm fallback)"
        rule_spec.reasoning.append(f"LLM interpretation unavailable ({str(exc)[:80]}); used rules")
        return rule_spec
