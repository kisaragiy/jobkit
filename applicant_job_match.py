"""Pure per-job match evidence: keyword/skill hits over title + JD text.

No browser, network or storage. Every conclusion this module returns can be
traced back to a text fragment of the record it was given; nothing is inferred
from names or invented, and a job without a JD body is reported as
``jd_status='missing'`` so the caller can keep it in human review instead of
treating a title-only hit as a match.
"""

import re

JD_MISSING = 'jd_missing'
KEYWORD_MISMATCH = 'KEYWORD_MISMATCH'
MAX_JD = 4000          # bounded: a JD body is data, not a channel to grow state
JD_EXCERPT = 240       # evidence snippet kept for human spot checks

SKILL_SPLIT = re.compile(r'[、,，;；/|·\s]+')


def _text(value):
    return value.strip()[:MAX_JD] if isinstance(value, str) else ''


def _boundary(keyword):
    """ASCII keywords need word boundaries so 'AI' does not hit 'paid'."""
    return (r'(?<![A-Za-z0-9])' + re.escape(keyword) + r'(?![A-Za-z0-9])'
            if keyword.isascii() else re.escape(keyword))


def keyword_hits(keywords, title, jd):
    """[(keyword, 'title'|'jd')] for every planned keyword that appears verbatim."""
    hits = []
    for keyword in keywords:
        key = _text(keyword)
        if not key:
            continue
        pattern = _boundary(key)
        if re.search(pattern, title, re.I):
            hits.append((key, 'title'))
        elif jd and re.search(pattern, jd, re.I):
            hits.append((key, 'jd'))
    return hits


def skill_matches(profile, jd, limit=12):
    """Profile skills that appear verbatim in the JD body; no synonyms, no guessing."""
    raw = profile.get('skills', '') if isinstance(profile.get('skills', ''), str) else ''
    names = [item.strip()[:40] for item in SKILL_SPLIT.split(raw) if len(item.strip()) >= 2]
    found = []
    for name in names:
        if jd and re.search(_boundary(name), jd, re.I) and name not in found:
            found.append(name)
        if len(found) >= limit:
            break
    return found


def evidence(job, jd, hits, skills):
    """Original fragments behind this conclusion, trimmed for human spot checks."""
    return {
        'title_text': _text(job.get('title'))[:200],
        'company_text': _text(job.get('company'))[:160],
        'city_text': _text(job.get('city'))[:40],
        'salary_text': _text(job.get('salary'))[:80],
        'jd_present': bool(jd),
        'jd_chars': len(jd),
        'jd_excerpt': jd[:JD_EXCERPT] if jd else None,
        'matched_keywords': [keyword for keyword, _ in hits],
        'matched_in': {keyword: field for keyword, field in hits},
        'skill_matches': list(skills),
    }


def assess(plan, profile, job):
    """One record -> matched keywords, skill hits, JD status and review codes.

    ``jd_status='missing'`` always carries ``jd_missing`` in ``review_codes``:
    the caller must not turn that into an "eligible/contact now" decision.
    """
    title = _text(job.get('title'))[:200]
    jd = _text(job.get('jd'))
    hits = keyword_hits(plan.get('keywords', []), title, jd)
    skills = skill_matches(profile if isinstance(profile, dict) else {}, jd)
    review_codes = [] if jd else [JD_MISSING]
    if not hits:
        review_codes = [KEYWORD_MISMATCH] + review_codes
    return {
        'jd_status': 'present' if jd else 'missing',
        'matched_keywords': [keyword for keyword, _ in hits],
        'matched_in': {keyword: field for keyword, field in hits},
        'skill_matches': skills,
        'review_codes': review_codes,
        'evidence': evidence(job, jd, hits, skills),
    }
