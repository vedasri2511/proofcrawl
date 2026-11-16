"""Approved-tool registry.

The ONLY way the backend can start a process is through a ToolSpec in this
registry. Each tool declares a fixed executable + module and a function that
turns a validated TaskSpec into an argv *list* (never a shell string). User or
model text never becomes part of a command except as a single, validated
argument value (the URL), which is re-validated by the CLI itself.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from . import config
from .intent import TaskSpec


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    why: str
    executable: str
    module: str
    build_args: Callable[[TaskSpec, str], list[str]]
    exit_codes: dict


def _sitecrawl_args(task: TaskSpec, out_path: str) -> list[str]:
    args = [
        "--url", task.url,
        "--out", out_path,
        "--max-pages", str(task.max_pages),
        "--max-depth", str(task.max_depth),
        "--max-seconds", str(config.CRAWL_BUDGET_S),
        "--request-timeout", "10",
        "--delay", "0.3",
        "--summary-sentences", "6",
    ]
    if task.check_links:
        args += ["--check-links", "--max-link-checks", "40"]
    if config.ALLOW_PRIVATE_HOSTS:
        args.append("--allow-private")
    return args


SITECRAWL = ToolSpec(
    name="sitecrawl",
    description="Bounded, robots-aware website crawler that writes a structured JSON result and streams NDJSON progress.",
    why="Purpose-built for this task: deterministic, no browser needed, explicit exit codes, SSRF guard, "
        "page/depth/time/byte limits, prompt-injection quarantine, and a checkpointed output file for partial results.",
    executable=sys.executable,
    module="sitecrawl",
    build_args=_sitecrawl_args,
    exit_codes={0: "success", 1: "internal error", 2: "invalid arguments", 3: "partial result",
                4: "site unreachable", 5: "blocked by robots.txt", 6: "blocked by security policy"},
)

REGISTRY: dict[str, ToolSpec] = {"sitecrawl": SITECRAWL}
INTENT_TO_TOOL = {"crawl_site": "sitecrawl"}


def build_argv(tool: ToolSpec, task: TaskSpec, out_path: str) -> list[str]:
    return [tool.executable, "-m", tool.module, *tool.build_args(task, out_path)]


def describe() -> list[dict]:
    return [{"name": t.name, "description": t.description, "why": t.why,
             "exit_codes": {str(k): v for k, v in t.exit_codes.items()}} for t in REGISTRY.values()]
