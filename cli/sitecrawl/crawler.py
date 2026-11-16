"""Polite, bounded, relevance-ranked website crawler."""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from . import __version__
from .extract import parse_page
from .guard import GuardError, UnreachableError, validate_url
from .robots import Robots
from .summarize import summarize

USER_AGENT = f"ProofCrawlBot/{__version__} (+https://github.com/; educational demo; respects robots.txt)"
IMPORTANT_HINTS = re.compile(r"about|company|who-we-are|team|contact|services?|products?|pricing|features|solutions|faq|mission|careers|docs|overview", re.I)
LOW_VALUE = re.compile(r"login|signin|sign-in|register|cart|checkout|account|logout|wp-admin|/tag/|/page/\d+|\?replytocom|privacy|terms|cookie|\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|mp3|css|js|xml|ico|webp)$", re.I)

EXIT_OK, EXIT_INTERNAL, EXIT_ARGS, EXIT_PARTIAL, EXIT_UNREACHABLE, EXIT_ROBOTS, EXIT_POLICY = 0, 1, 2, 3, 4, 5, 6


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def emit(event: str, **data) -> None:
    """Machine-readable progress on stdout (NDJSON). The backend parses these."""
    sys.stdout.write(json.dumps({"event": event, "ts": now_iso(), **data}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    """Human-readable diagnostics on stderr."""
    sys.stderr.write(f"[sitecrawl {datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def normalise(url: str) -> str:
    p = urlsplit(url)
    path = p.path or "/"
    return urlunsplit((p.scheme.lower(), (p.netloc or "").lower(), path, p.query, ""))


class Crawler:
    def __init__(self, args):
        self.a = args
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
        self.started = time.monotonic()
        self.started_iso = now_iso()
        self.pages, self.errors, self.skipped = [], [], []
        self.raw_for_summary = []
        self.bytes_total = 0
        self.robots = None
        self.robots_info = {}
        self.stop_reason = None

    # ------------------------------------------------------------------ fetch
    def fetch(self, url: str):
        """GET with manual redirects so every hop is re-validated (SSRF-safe)."""
        current = url
        for hop in range(6):
            validate_url(current, self.a.allow_private)
            t0 = time.monotonic()
            r = self.session.get(current, timeout=(5, self.a.request_timeout), allow_redirects=False, stream=True)
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                r.close()
                if not loc:
                    break
                current = urljoin(current, loc)
                continue
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > self.a.max_bytes:
                    body = body[: self.a.max_bytes]
                    break
            r.close()
            ms = int((time.monotonic() - t0) * 1000)
            return r, current, body, ms
        raise GuardError("too many redirects")

    # ----------------------------------------------------------------- robots
    def load_robots(self, start_url: str) -> bool:
        p = urlsplit(start_url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        rp = Robots()
        info = {"url": robots_url, "fetched": False, "status": None, "crawl_delay": None}
        try:
            r, _, body, _ = self.fetch(robots_url)
            info["status"] = r.status_code
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp = Robots(body.decode("utf-8", "replace"))
                info["fetched"] = True
                info["rules_for_us"] = len(rp.rules)
        except (requests.RequestException, GuardError) as exc:
            info["error"] = str(exc)[:200]
            rp.allow_all = True
        info["crawl_delay"] = rp.crawl_delay
        self.robots, self.robots_info = rp, info
        allowed = rp.can_fetch(start_url)
        info["start_url_allowed"] = allowed
        emit("robots", **info)
        return allowed

    # ----------------------------------------------------------------- crawl
    def priority(self, url: str, text: str, depth: int) -> float:
        score = 10.0 - depth * 3
        if IMPORTANT_HINTS.search(urlsplit(url).path) or IMPORTANT_HINTS.search(text or ""):
            score += 4
        score -= len(urlsplit(url).path.strip("/").split("/")) * 0.3
        return score

    def run(self) -> int:
        start = validate_url(self.a.url, self.a.allow_private)
        start = normalise(start)
        root_host = urlsplit(start).hostname
        emit("start", url=start, limits=self.limits())
        log(f"starting crawl of {start}")

        if not self.load_robots(start):
            self.stop_reason = "robots_disallowed"
            emit("blocked", reason="robots.txt disallows crawling the start URL")
            self.write_result("failed")
            return EXIT_ROBOTS

        delay = max(self.a.delay, min(float(self.robots_info.get("crawl_delay") or 0), 5.0))
        frontier, seq, seen = [], 0, {start}
        heapq.heappush(frontier, (-100.0, seq, start, 0))

        while frontier and len(self.pages) < self.a.max_pages:
            if time.monotonic() - self.started > self.a.max_seconds:
                self.stop_reason = "time_budget_exhausted"
                emit("warning", message=f"time budget of {self.a.max_seconds}s reached; returning partial result")
                break
            _, _, url, depth = heapq.heappop(frontier)
            if not self.robots.can_fetch(url):
                self.skipped.append({"url": url, "reason": "robots.txt"})
                emit("skip", url=url, reason="robots.txt")
                continue
            emit("fetching", url=url, depth=depth, queue=len(frontier))
            try:
                resp, final_url, body, ms = self.fetch(url)
            except UnreachableError as exc:
                self.errors.append({"url": url, "error": str(exc)})
                emit("page_error", url=url, error=str(exc))
                continue
            except GuardError as exc:
                self.skipped.append({"url": url, "reason": f"security policy: {exc}"})
                emit("skip", url=url, reason=f"security policy: {exc}")
                if not self.pages and url == start:
                    self.write_result("failed", fatal=str(exc))
                    return EXIT_POLICY
                continue
            except requests.RequestException as exc:
                err = type(exc).__name__ + ": " + str(exc)[:160]
                self.errors.append({"url": url, "error": err})
                emit("page_error", url=url, error=err)
                log(f"error fetching {url}: {err}")
                continue

            self.bytes_total += len(body)
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code >= 400:
                self.errors.append({"url": url, "error": f"HTTP {resp.status_code}"})
                emit("page_error", url=url, error=f"HTTP {resp.status_code}", status=resp.status_code)
                continue
            if "html" not in ctype.lower():
                self.skipped.append({"url": url, "reason": f"content-type {ctype or 'unknown'}"})
                emit("skip", url=url, reason=f"non-HTML content ({ctype or 'unknown'})")
                continue
            enc = resp.encoding if "charset" in ctype.lower() else "utf-8"
            html = body.decode(enc or "utf-8", "replace")
            try:
                parsed = parse_page(html, final_url)
            except Exception as exc:  # one malformed page must not kill the whole crawl
                err = f"could not parse page: {type(exc).__name__}: {exc}"[:200]
                self.errors.append({"url": url, "error": err})
                emit("page_error", url=url, error=err)
                log(f"parse error on {url}: {err}")
                continue
            sha = hashlib.sha256(body).hexdigest()
            page = {
                "url": url,
                "final_url": final_url,
                "status_code": resp.status_code,
                "depth": depth,
                "title": parsed["metadata"]["title"],
                "description": parsed["metadata"]["description"],
                "language": parsed["metadata"]["language"],
                "headings": parsed["headings"][:12],
                "word_count": parsed["word_count"],
                "excerpt": parsed["text"][:400],
                "internal_links": len(parsed["links"]["internal"]),
                "external_links": len(parsed["links"]["external"]),
                "images": parsed["images"],
                "bytes": len(body),
                "fetch_ms": ms,
                "sha256": sha,
                "_parsed": parsed,
            }
            self.pages.append(page)
            self.raw_for_summary.append({"url": final_url, "depth": depth, "text": parsed["text"], "metadata": parsed["metadata"]})
            emit("page", url=url, status=resp.status_code, depth=depth, title=page["title"], words=page["word_count"],
                 bytes=len(body), ms=ms, sha256=sha[:16], injection_flags=len(parsed["injection_findings"]))
            for f in parsed["injection_findings"]:
                emit("injection_detected", url=url, rule=f["rule"], location=f["location"], snippet=f["snippet"][:160])
            self.write_result("running")  # checkpoint: partial result survives a kill/timeout

            if depth < self.a.max_depth:
                for link in parsed["links"]["internal"]:
                    nu = normalise(link["url"])
                    if nu in seen or urlsplit(nu).hostname != root_host or LOW_VALUE.search(nu):
                        continue
                    seen.add(nu)
                    seq += 1
                    heapq.heappush(frontier, (-self.priority(nu, link["text"], depth + 1), seq, nu, depth + 1))
            if delay:
                time.sleep(delay)

        if frontier and len(self.pages) >= self.a.max_pages and not self.stop_reason:
            self.stop_reason = "page_limit_reached"
        broken = self.check_links() if self.a.check_links and self.pages else None

        if not self.pages:
            self.write_result("failed", fatal=(self.errors[0]["error"] if self.errors else "no HTML pages could be fetched"))
            emit("failed", reason="no pages could be crawled")
            return EXIT_UNREACHABLE
        hard_errors = [e for e in self.errors if not e["error"].startswith("HTTP 4")]
        status = "partial" if (hard_errors or self.stop_reason == "time_budget_exhausted") else "success"
        self.write_result(status, broken=broken)
        return EXIT_OK if status == "success" else EXIT_PARTIAL

    def check_links(self):
        links = []
        for p in self.pages:
            for l in p["_parsed"]["links"]["internal"] + p["_parsed"]["links"]["external"]:
                if l["url"] not in [x[0] for x in links]:
                    links.append((l["url"], p["url"]))
        broken = []
        emit("link_check", total=min(len(links), self.a.max_link_checks))
        for u, found_on in links[: self.a.max_link_checks]:
            try:
                validate_url(u, self.a.allow_private)
                r = self.session.head(u, timeout=(4, 6), allow_redirects=True)
                if r.status_code in (405, 403):
                    r = self.session.get(u, timeout=(4, 6), stream=True)
                    r.close()
                if r.status_code >= 400:
                    broken.append({"url": u, "status": r.status_code, "found_on": found_on})
            except GuardError as exc:
                continue
            except requests.RequestException as exc:
                broken.append({"url": u, "status": None, "error": type(exc).__name__, "found_on": found_on})
        emit("link_check_done", broken=len(broken))
        return broken

    # ---------------------------------------------------------------- output
    def limits(self):
        return {k: getattr(self.a, k) for k in ("max_pages", "max_depth", "max_seconds", "request_timeout", "max_bytes", "delay")}

    def write_result(self, status: str, fatal: str | None = None, broken=None):
        pages = self.pages
        site = self.site_profile()
        summary = summarize(self.raw_for_summary, self.a.summary_sentences) if self.raw_for_summary else None
        internal, external = {}, {}
        findings, hidden = [], 0
        for p in pages:
            pr = p["_parsed"]
            for l in pr["links"]["internal"]:
                internal.setdefault(l["url"], l["text"])
            for l in pr["links"]["external"]:
                external.setdefault(l["url"], l["text"])
            hidden += pr["hidden_text_blocks"]
            for f in pr["injection_findings"]:
                findings.append({"page": p["url"], **f})
        result = {
            "schema_version": "1.0",
            "tool": {"name": "sitecrawl", "version": __version__},
            "status": status,
            "stop_reason": self.stop_reason,
            "fatal_error": fatal,
            "start_url": self.a.url,
            "started_at": self.started_iso,
            "finished_at": now_iso() if status != "running" else None,
            "duration_ms": int((time.monotonic() - self.started) * 1000),
            "limits": self.limits(),
            "robots": self.robots_info,
            "site": site,
            "summary": summary,
            "pages": [{k: v for k, v in p.items() if k != "_parsed"} for p in pages],
            "links": {
                "internal": [{"url": u, "text": t} for u, t in list(internal.items())[:150]],
                "external": [{"url": u, "text": t} for u, t in list(external.items())[:100]],
            },
            "broken_links": broken,
            "errors": self.errors,
            "skipped": self.skipped[:100],
            "security": {
                "injection_findings": findings[:50],
                "pages_with_injection": len({f["page"] for f in findings}),
                "quarantined_sentences": summary["quarantined_sentences"] if summary else 0,
                "hidden_text_blocks_removed": hidden,
                "policy": "website content treated as untrusted data; flagged sentences excluded from summary",
            },
            "stats": {"pages_crawled": len(pages), "pages_failed": len(self.errors), "pages_skipped": len(self.skipped),
                      "bytes_downloaded": self.bytes_total, "total_words": sum(p["word_count"] for p in pages)},
        }
        tmp = self.a.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.a.out)  # atomic: readers never see a half-written file
        if status != "running":
            with open(self.a.out, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            emit("done", status=status, pages=len(pages), errors=len(self.errors), out=os.path.basename(self.a.out), sha256=digest)

    def site_profile(self):
        if not self.pages:
            return None
        home = self.pages[0]["_parsed"]
        md = home["metadata"]
        org = None
        for p in self.pages:
            for sd in p["_parsed"]["structured_data"]:
                if str(sd.get("@type", "")).lower() in ("organization", "corporation", "localbusiness", "website", "educationalorganization"):
                    org = {k: sd[k] for k in ("@type", "name", "url", "description", "address", "telephone", "email", "sameAs") if k in sd}
                    break
            if org:
                break
        emails, phones, social = set(), {}, {}
        for p in self.pages:
            c = p["_parsed"]["contacts"]
            emails.update(e.lower() for e in c["emails"])
            for ph in c["phones"]:
                phones.setdefault(re.sub(r"\D", "", ph)[-10:], ph)
            for k, v in c["social"].items():
                social.setdefault(k, v)
        name = (org or {}).get("name") or md.get("site_name") or md.get("og_title") or md.get("title")
        return {
            "name": name,
            "title": md.get("title"),
            "description": md.get("description"),
            "language": md.get("language"),
            "organization": org,
            "contacts": {"emails": sorted(emails)[:20], "phones": sorted(phones.values())[:20], "social": social},
            "top_headings": [h["text"] for h in home["headings"][:8]],
        }
