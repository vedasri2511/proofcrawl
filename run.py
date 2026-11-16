"""One-command launcher.

    python run.py                 # start ProofCrawl on http://127.0.0.1:8000
    python run.py --demo-site     # also serve the bundled demo website on :8765
                                  # (and allow the crawler to reach localhost)
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def serve_demo_site(port: int):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(ROOT / "demo_site"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"  demo site      : http://localhost:{port}/   (fictional 'Verdant Grid Cooperative')")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo-site", action="store_true", help="serve demo_site/ on :8765 and allow localhost crawling")
    ap.add_argument("--demo-port", type=int, default=8765)
    a = ap.parse_args()
    load_dotenv()
    print("ProofCrawl - chat-driven CLI execution with proof")
    if a.demo_site:
        os.environ["PROOFCRAWL_ALLOW_PRIVATE_HOSTS"] = "1"
        serve_demo_site(a.demo_port)
    import uvicorn
    from backend import config
    print(f"  chat UI        : http://{a.host}:{a.port}/")
    print(f"  LLM            : {'enabled (' + config.LLM_MODEL + ')' if config.LLM_ENABLED else 'not configured - using deterministic rule parser'}")
    print(f"  private hosts  : {'ALLOWED (demo mode)' if config.ALLOW_PRIVATE_HOSTS else 'blocked'}")
    uvicorn.run("backend.app:app", host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
