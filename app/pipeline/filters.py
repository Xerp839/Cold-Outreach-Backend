"""Free, deterministic filters that run before any LLM call."""

import re

from app.config import Settings
from app.pipeline.fetch import INDIA_HINTS, RawJob

_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_PLUS_RE = re.compile(r"(?:minimum|min\.?|at\s*least)?\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_EXP_CONTEXT = re.compile(r"experience|exp\b|industry|hands[- ]on|professional|working", re.I)


def _kw_pattern(keywords: list[str]) -> re.Pattern | None:
    words = [re.escape(k.lower().strip()) for k in keywords if k.strip()]
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + r")(?![a-z0-9])")


def min_required_years(experience_text: str | None, description: str | None) -> int | None:
    """Lowest 'minimum years' the posting asks for. None when it doesn't say."""
    if experience_text:
        m = re.search(r"(\d{1,2})", experience_text)
        if m:
            return int(m.group(1))
    if not description:
        return None
    mins = []
    for m in _RANGE_RE.finditer(description):
        mins.append(int(m.group(1)))
    for m in _PLUS_RE.finditer(description):
        window = description[max(0, m.start() - 60): m.end() + 60]
        if _EXP_CONTEXT.search(window):
            mins.append(int(m.group(1)))
    mins = [y for y in mins if y <= 20]
    return min(mins) if mins else None


def rule_filter(job: RawJob, settings: Settings) -> str | None:
    """Returns a rejection reason, or None if the job should go to LLM scoring."""
    title = job.title.lower()
    company = job.company.lower()

    for blocked in settings.company_blocklist:
        if blocked.lower() in company:
            return f"company blocklisted ({blocked})"

    exclude = _kw_pattern(settings.title_exclude)
    if exclude and (m := exclude.search(title)):
        return f"title excluded ('{m.group(0)}')"
    include = _kw_pattern(settings.title_include)
    if include and not include.search(title):
        return "title doesn't match target roles"

    years = min_required_years(job.experience_text, job.description)
    if years is not None and years > settings.max_required_experience:
        return f"asks for {years}+ years"

    if job.salary_max_lpa is not None and job.salary_max_lpa < settings.min_ctc_lpa:
        return f"pays at most {job.salary_max_lpa} LPA"

    loc = (job.location or "").lower()
    if loc and not job.is_remote and not any(h in loc for h in INDIA_HINTS):
        return f"location outside India ({job.location})"

    if not job.description or len(job.description) < 200:
        return "no usable job description"
    return None
