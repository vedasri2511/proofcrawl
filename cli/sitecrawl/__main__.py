"""CLI entry point.

    python -m sitecrawl --url https://example.com --out result.json [options]

Contract with callers (the ProofCrawl backend or a human in a terminal):
  stdout : NDJSON progress events, one JSON object per line
  stderr : human-readable diagnostics
  --out  : the final structured result (JSON, written atomically; also
           checkpointed after every page so partial results survive a kill)
  exit   : 0 success | 1 internal error | 2 invalid arguments | 3 partial
           4 unreachable / no pages | 5 blocked by robots.txt | 6 blocked by security policy
"""
from __future__ import annotations

import argparse
import sys
import traceback

from .crawler import EXIT_ARGS, EXIT_INTERNAL, EXIT_POLICY, EXIT_UNREACHABLE, Crawler, emit, log
from .guard import GuardError, UnreachableError, validate_url


def bounded(lo, hi, cast=int):
    def f(v):
        x = cast(v)
        if not lo <= x <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return x
    return f


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sitecrawl", description="Bounded, polite website crawler that emits structured JSON.")
    p.add_argument("--url", required=True, help="start URL (http/https)")
    p.add_argument("--out", required=True, help="path of the JSON result file")
    p.add_argument("--max-pages", type=bounded(1, 50), default=8)
    p.add_argument("--max-depth", type=bounded(0, 4), default=2)
    p.add_argument("--max-seconds", type=bounded(5, 300), default=60, help="crawl time budget")
    p.add_argument("--request-timeout", type=bounded(2, 30, float), default=10.0)
    p.add_argument("--max-bytes", type=bounded(10_000, 5_000_000), default=2_000_000, help="per-page download cap")
    p.add_argument("--delay", type=bounded(0, 5, float), default=0.3, help="politeness delay between requests (s)")
    p.add_argument("--summary-sentences", type=bounded(2, 12), default=6)
    p.add_argument("--check-links", action="store_true", help="also check discovered links for breakage")
    p.add_argument("--max-link-checks", type=bounded(1, 100), default=40)
    p.add_argument("--allow-private", action="store_true", help="DEV ONLY: allow localhost/private hosts (for the bundled demo site)")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_ARGS if e.code else 0
    try:
        validate_url(args.url, args.allow_private)
    except UnreachableError as exc:
        emit("failed", reason=f"site unreachable: {exc}")
        log(f"unreachable: {exc}")
        return EXIT_UNREACHABLE
    except GuardError as exc:
        emit("blocked", reason=f"security policy: {exc}")
        log(f"refusing URL: {exc}")
        return EXIT_POLICY
    try:
        return Crawler(args).run()
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except Exception as exc:  # never die silently: report on both channels
        emit("crashed", error=f"{type(exc).__name__}: {exc}"[:300])
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
