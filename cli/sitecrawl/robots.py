"""robots.txt parser following RFC 9309.

Written in-house because urllib.robotparser behaves differently across Python
versions (older versions turn `Disallow: /?` into `Disallow: /`, which wrongly
blocks entire sites such as google.com). Rules:
  * the group whose user-agent token matches ours is used, else the `*` group
  * the longest matching rule wins; on a tie, Allow wins
  * `*` matches any characters, `$` anchors the end
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def _compile(pattern: str):
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if c == "*" else re.escape(c) for c in body)
    return re.compile(rx + ("$" if anchored else ""), re.DOTALL)


class Robots:
    def __init__(self, text: str = "", agent_token: str = "proofcrawlbot"):
        self.allow_all = False
        self.disallow_all = False
        self.rules: list[tuple[int, bool, re.Pattern]] = []
        self.crawl_delay: float | None = None
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[str, str]] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow", "crawl-delay"):
                rules.append((key, val))
                last_was_agent = False
        if agents or rules:
            groups.append((agents, rules))

        token = agent_token.lower()
        chosen = [r for a, r in groups if token in a] or [r for a, r in groups if "*" in a]
        for group_rules in chosen:
            for key, val in group_rules:
                if key == "crawl-delay":
                    try:
                        self.crawl_delay = float(val)
                    except ValueError:
                        pass
                elif val:  # empty Disallow means "allow everything"
                    self.rules.append((len(val), key == "allow", _compile(val)))

    def can_fetch(self, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        p = urlsplit(url)
        target = (p.path or "/") + (f"?{p.query}" if p.query else "")
        best_len, allowed = -1, True
        for length, allow, rx in self.rules:
            if rx.match(target) and (length > best_len or (length == best_len and allow)):
                best_len, allowed = length, allow
        return allowed
