"""
matcher.py — keyword matching (simple, fast, "one keyword is enough").
======================================================================

Checking a message against the keyword list is extremely cheap, so we just do
a case-insensitive substring scan over all keywords in one pass. Logic is OR:
a single matching keyword is enough to flag a candidate, and ``match`` returns
*every* keyword that hit (there can be several).

The keyword set is loaded once and rebuilt whenever the panel changes it, so
matching never touches the database on the hot path.
"""
from __future__ import annotations

import threading

from core import db

_lock = threading.Lock()
_keywords: list = []          # original-cased keywords (for nice display)
_keywords_lc: list = []       # lower-cased, for matching


def rebuild(words=None) -> int:
    """Load keywords from the DB (or use the provided list) and cache them.
    Returns how many keywords are active."""
    global _keywords, _keywords_lc
    if words is None:
        words = db.list_keywords()
    with _lock:
        _keywords = [w for w in words if w and w.strip()]
        _keywords_lc = [w.lower() for w in _keywords]
    return len(_keywords)


def count() -> int:
    with _lock:
        return len(_keywords)


def match(text: str) -> list:
    """Return the list of keywords found in ``text`` (case-insensitive,
    substring). Empty list means no match."""
    if not text:
        return []
    low = text.lower()
    with _lock:
        kws, kws_lc = _keywords, _keywords_lc
    hits = []
    for original, lc in zip(kws, kws_lc):
        if lc in low:
            hits.append(original)
    return hits
