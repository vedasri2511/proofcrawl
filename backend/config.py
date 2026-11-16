"""Runtime configuration (environment variables, all optional)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "cli"
FRONTEND_DIR = ROOT / "frontend"
RUNS_DIR = Path(os.getenv("PROOFCRAWL_RUNS_DIR", ROOT / "runs"))


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Execution limits (server-side caps; the user/LLM can ask for less, never more)
MAX_PAGES_CAP = int(os.getenv("PROOFCRAWL_MAX_PAGES_CAP", 25))
MAX_DEPTH_CAP = int(os.getenv("PROOFCRAWL_MAX_DEPTH_CAP", 3))
DEFAULT_PAGES = int(os.getenv("PROOFCRAWL_DEFAULT_PAGES", 8))
DEFAULT_DEPTH = int(os.getenv("PROOFCRAWL_DEFAULT_DEPTH", 2))
CRAWL_BUDGET_S = int(os.getenv("PROOFCRAWL_CRAWL_BUDGET_S", 60))      # passed to the CLI
HARD_TIMEOUT_S = int(os.getenv("PROOFCRAWL_HARD_TIMEOUT_S", 90))      # backend kills the process after this
MAX_CONCURRENT_RUNS = int(os.getenv("PROOFCRAWL_MAX_CONCURRENT_RUNS", 2))
ALLOW_PRIVATE_HOSTS = _bool("PROOFCRAWL_ALLOW_PRIVATE_HOSTS", False)  # only for the local demo site

# Optional LLM (any OpenAI-compatible endpoint: OpenAI, Groq, OpenRouter, Gemini-compat, Ollama...)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_ENABLED = bool(LLM_BASE_URL and LLM_MODEL)
