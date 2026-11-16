"""HTML -> structured page record. Pure functions, no network."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from .guard import scan_text

EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}\b")
PHONE_RX = re.compile(r"(?<!\w)(\+?\d{1,3}[\s.-]?)?(\(?\d{2,5}\)?[\s.-]?)\d{3,4}[\s.-]?\d{3,4}(?!\w)")
SOCIAL_DOMAINS = {
    "twitter.com": "twitter", "x.com": "twitter", "linkedin.com": "linkedin",
    "facebook.com": "facebook", "instagram.com": "instagram", "github.com": "github",
    "youtube.com": "youtube", "tiktok.com": "tiktok", "medium.com": "medium",
}
HIDDEN_STYLE_RX = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(px|pt|em)?\s*(;|$)|opacity\s*:\s*0(\.0+)?\s*(;|$)"
    r"|color\s*:\s*(#fff(fff)?|white)\b[^;]*;?\s*background(-color)?\s*:\s*(#fff(fff)?|white)|left\s*:\s*-\d{3,}px",
    re.IGNORECASE,
)
NOISE_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "canvas", "form"]


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def parse_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # ---- hidden content (classic prompt-injection hiding place) -------------
    hidden_texts = []
    for el in soup.find_all(True):
        if el.attrs is None or getattr(el, "decomposed", False):
            continue  # already removed together with a hidden parent
        style = el.get("style") or ""
        if not isinstance(style, str):
            style = " ".join(style)
        if el.get("hidden") is not None or el.get("aria-hidden") == "true" or HIDDEN_STYLE_RX.search(style):
            t = _clean(el.get_text(" "))
            if t:
                hidden_texts.append(t)
            el.decompose()
    comments = [_clean(c) for c in soup.find_all(string=lambda s: isinstance(s, Comment))]
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    # ---- metadata -----------------------------------------------------------
    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
            if tag and tag.get("content"):
                return _clean(tag["content"])[:500]
        return None

    title = _clean(soup.title.string if soup.title and soup.title.string else "")[:300] or None
    canonical = soup.find("link", rel="canonical")
    html_tag = soup.find("html")
    metadata = {
        "title": title,
        "description": meta("description", "og:description", "twitter:description"),
        "site_name": meta("og:site_name", "application-name"),
        "og_title": meta("og:title"),
        "keywords": meta("keywords"),
        "canonical": canonical.get("href") if canonical else None,
        "language": html_tag.get("lang") if html_tag else None,
    }

    # ---- JSON-LD structured data -------------------------------------------
    structured = []
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items[:10]:
            if isinstance(it, dict):
                structured.append({k: it[k] for k in list(it)[:15] if not k.startswith("@context")})

    # ---- links (before removing nav/footer) --------------------------------
    host = urlsplit(url).hostname or ""
    internal, external, social = [], [], {}
    emails, phones = set(), set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("mailto:"):
            emails.add(href[7:].split("?")[0].strip())
            continue
        if href.startswith("tel:"):
            phones.add(_clean(href[4:]))
            continue
        if href.startswith(("javascript:", "#", "data:")):
            continue
        absu = urljoin(url, href).split("#")[0]
        h = (urlsplit(absu).hostname or "").lower()
        if urlsplit(absu).scheme not in ("http", "https"):
            continue
        text = _clean(a.get_text(" "))[:120]
        if h == host or h.endswith("." + host) or host.endswith("." + h):
            internal.append({"url": absu, "text": text})
        else:
            external.append({"url": absu, "text": text})
            base = h[4:] if h.startswith("www.") else h
            if base in SOCIAL_DOMAINS:
                social.setdefault(SOCIAL_DOMAINS[base], absu)

    for t in soup(NOISE_TAGS):
        t.decompose()

    headings = []
    for lvl in ("h1", "h2", "h3"):
        for h in soup.find_all(lvl):
            t = _clean(h.get_text(" "))
            if t:
                headings.append({"level": lvl, "text": t[:200]})

    # main text: prefer <main>/<article>, else body
    main = soup.find("main") or soup.find("article") or soup.body or soup
    blocks = []
    for el in main.find_all(["p", "li", "h1", "h2", "h3", "h4", "td", "blockquote", "dd"]):
        t = _clean(el.get_text(" "))
        if len(t) >= 3:
            blocks.append(t)
    text = "\n".join(dict.fromkeys(blocks))  # dedupe, keep order
    if not text:
        text = _clean(main.get_text(" "))

    emails |= set(EMAIL_RX.findall(text))
    for m in PHONE_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 13:
            phones.add(_clean(m.group(0)))

    # ---- injection scan over visible text, hidden text and comments ---------
    findings = []
    for f in scan_text(text):
        findings.append({**f, "location": "visible_text"})
    for t in hidden_texts:
        for f in scan_text(t):
            findings.append({**f, "location": "hidden_element"})
    for t in comments:
        for f in scan_text(t):
            findings.append({**f, "location": "html_comment"})

    return {
        "metadata": metadata,
        "headings": headings[:40],
        "text": text[:60000],
        "word_count": len(text.split()),
        "links": {"internal": _dedupe(internal)[:200], "external": _dedupe(external)[:100]},
        "contacts": {"emails": sorted(emails)[:20], "phones": sorted(phones)[:20], "social": social},
        "structured_data": structured[:10],
        "images": len(soup.find_all("img")),
        "hidden_text_blocks": len(hidden_texts),
        "injection_findings": findings[:20],
    }


def _dedupe(links: list[dict]) -> list[dict]:
    seen, out = set(), []
    for l in links:
        if l["url"] not in seen:
            seen.add(l["url"])
            out.append(l)
    return out
