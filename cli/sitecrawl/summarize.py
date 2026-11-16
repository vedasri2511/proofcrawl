"""Deterministic, evidence-linked extractive summariser.

Every summary sentence is a real sentence taken from a crawled page and carries
the URL it came from, so the user can verify each claim. Sentences that were
flagged by the injection scanner are quarantined and never selected.
Boilerplate (sentences repeated across many pages: nav, footer, cookie banners)
is down-weighted.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .guard import is_suspicious_sentence

STOP = set("""a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers herself him himself his how i if in into is it its itself just let me more most my myself no nor not now of off on
once only or other our ours ourselves out over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up very was we were what when where which while
who whom why will with would you your yours yourself yourselves us use used using get got via per one two may new
cookie cookies click here read more learn privacy policy terms rights reserved copyright""".split())
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
WORD = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")


def _sentences(text: str) -> list[str]:
    out = []
    for block in text.split("\n"):
        for s in SENT_SPLIT.split(block):
            s = s.strip()
            if 8 <= len(s.split()) <= 60 and not s.endswith(":"):
                out.append(s)
    return out


def _tokens(s: str) -> list[str]:
    return [w.lower() for w in WORD.findall(s) if w.lower() not in STOP]


def summarize(pages: list[dict], max_sentences: int = 6) -> dict:
    """pages: [{url, depth, text, metadata}] -> summary dict."""
    cands, seen_count = [], Counter()
    quarantined = 0
    for p in pages:
        for s in set(_sentences(p.get("text", ""))):
            seen_count[s] += 1
    df, tf, n_pages = Counter(), Counter(), max(1, len(pages))
    for p in pages:
        full = p.get("text", "") + " " + ((p.get("metadata") or {}).get("description") or "")
        df.update(set(_tokens(full)))
        tf.update(_tokens(full))

    for idx, p in enumerate(pages):
        desc = (p.get("metadata") or {}).get("description")
        sents = _sentences(p.get("text", ""))
        if desc and len(desc.split()) >= 6:
            sents = [desc] + sents
        for pos, s in enumerate(sents):
            if is_suspicious_sentence(s):
                quarantined += 1
                continue
            toks = _tokens(s)
            if len(toks) < 4:
                continue
            score = sum(math.log1p(tf[t]) * math.log(1 + n_pages / max(1, df[t])) for t in set(toks)) / math.sqrt(len(toks))
            score *= 1.0 / (1 + 0.15 * pos)               # earlier in page = more important
            score *= 1.0 / (1 + 0.5 * p.get("depth", 0))   # homepage / top pages first
            if seen_count[s] > 1 and n_pages > 2:
                score *= 0.2                               # boilerplate penalty
            if s == desc:
                score *= 1.6
            cands.append((score, s, p["url"], set(toks)))

    cands.sort(key=lambda c: -c[0])
    chosen = []
    for score, s, url, toks in cands:
        if any(len(toks & c[3]) / max(1, len(toks | c[3])) > 0.45 for c in chosen):
            continue  # diversity: skip near-duplicates
        chosen.append((score, s, url, toks))
        if len(chosen) >= max_sentences:
            break

    evidence = [{"id": i + 1, "sentence": s, "source_url": u, "score": round(sc, 3)} for i, (sc, s, u, _) in enumerate(chosen)]
    keywords = [w for w, _ in sorted(((t, tf[t] * math.log(1 + n_pages / df[t])) for t in tf if len(t) > 3), key=lambda x: -x[1])[:12]]
    return {
        "method": "extractive-tfidf-evidence-linked",
        "text": " ".join(f"{e['sentence']} [{e['id']}]" for e in evidence),
        "evidence": evidence,
        "key_topics": keywords,
        "quarantined_sentences": quarantined,
    }
