"""Job sources.

- JobSpy scrapes LinkedIn / Naukri / Indeed (free, no keys).
- Public ATS boards (Greenhouse / Lever / Ashby) for companies listed in config/companies.json.
  These are the most reliable source and come with the company's real domain.
"""

import html
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import Settings
from app.emails import clean_domain, domain_from_url, extract_emails

COMPANIES_FILE = Path(__file__).resolve().parents[2] / "config" / "companies.json"
INDIA_HINTS = (
    "india", "mumbai", "pune", "bengaluru", "bangalore", "hyderabad", "chennai", "delhi", "gurgaon", "gurugram",
    "noida", "kolkata", "ahmedabad", "navi mumbai", "thane", "remote",
)


@dataclass
class RawJob:
    source: str
    url: str
    title: str
    company: str
    external_id: str | None = None
    apply_url: str | None = None
    company_domain: str | None = None
    company_linkedin_url: str | None = None
    location: str | None = None
    is_remote: bool = False
    description: str | None = None
    experience_text: str | None = None
    salary_text: str | None = None
    salary_max_lpa: float | None = None
    jd_emails: list[str] = field(default_factory=list)
    posted_at: date | None = None

    @property
    def dedupe_key(self) -> str:
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        return f"{norm(self.company)}|{norm(self.title)}"[:300]


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        import pandas as pd

        if v is pd.NaT:
            return None
    except ImportError:
        pass
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    text = html.unescape(text)
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _to_lpa(amount, interval, currency) -> float | None:
    if amount is None or (currency and str(currency).upper() != "INR"):
        return None
    yearly = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}.get(str(interval or "yearly").lower(), 1)
    return round(float(amount) * yearly / 100_000, 1)


def _as_date(v) -> date | None:
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19]).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- JobSpy


def _linkedin_company_url(url) -> str | None:
    # JobSpy's company_url is the job board's company page; only LinkedIn's is worth keeping.
    url = str(url or "").split("?")[0].rstrip("/")
    return url if "linkedin.com/company/" in url else None


class _JobSpyErrors(logging.Handler):
    """JobSpy logs blocks (429 "use a proxy", "no jobs (blocked?)") instead of raising, and returns fewer jobs."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage()[:200])


def fetch_jobspy(settings: Settings, site: str, term: str, problems: list[str] | None = None) -> list[RawJob]:
    """`problems` collects what the site reported even when some jobs still came back."""
    from jobspy import scrape_jobs  # heavy import (pandas), keep it lazy

    errors = _JobSpyErrors()
    logging.getLogger("jobspy").addHandler(errors)
    try:
        df = scrape_jobs(
            site_name=[site],
            search_term=term,
            location=settings.search_location,
            results_wanted=settings.results_per_search,
            hours_old=settings.hours_old,
            country_indeed="India",
            fetch_description=True,
            description_format="markdown",
            verbose=0,
        )
    finally:
        logging.getLogger("jobspy").removeHandler(errors)
        if problems is not None:
            problems.extend(dict.fromkeys(errors.messages))
    return _jobspy_rows(df, site)


def _jobspy_rows(df, site: str) -> list[RawJob]:
    jobs = []
    for row in df.to_dict("records"):
        r = {k: _clean(v) for k, v in row.items()}
        if not r.get("title") or not r.get("company") or not r.get("job_url"):
            continue
        min_lpa = _to_lpa(r.get("min_amount"), r.get("interval"), r.get("currency"))
        max_lpa = _to_lpa(r.get("max_amount"), r.get("interval"), r.get("currency"))
        salary_text = f"{min_lpa or '?'}-{max_lpa or '?'} LPA" if (min_lpa or max_lpa) else None
        emails = r.get("emails")
        emails = [e.lower() for e in emails] if isinstance(emails, list) else extract_emails(r.get("description"))
        jobs.append(RawJob(
            source=site,
            external_id=str(r.get("id")) if r.get("id") else None,
            url=r["job_url"],
            apply_url=r.get("job_url_direct"),
            title=str(r["title"]).strip(),
            company=str(r["company"]).strip(),
            company_domain=domain_from_url(r.get("company_url_direct")) or domain_from_url(r.get("job_url_direct")),
            company_linkedin_url=_linkedin_company_url(r.get("company_url")),
            location=r.get("location"),
            is_remote=bool(r.get("is_remote")) or "remote" in str(r.get("work_from_home_type") or "").lower(),
            description=r.get("description"),
            experience_text=r.get("experience_range"),
            salary_text=salary_text,
            salary_max_lpa=max_lpa or min_lpa,
            jd_emails=emails,
            posted_at=_as_date(r.get("date_posted")),
        ))
    return jobs


# --------------------------------------------------------------------------- ATS boards


def _looks_india(location: str | None) -> bool:
    return bool(location) and any(h in location.lower() for h in INDIA_HINTS)


def _greenhouse(c: dict) -> list[RawJob]:
    data = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs", params={"content": "true"}, timeout=30).json()
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name")
        desc = _strip_html(j.get("content"))
        out.append(RawJob(
            source="greenhouse", external_id=str(j["id"]), url=j["absolute_url"], title=j["title"], company=c["name"],
            company_domain=clean_domain(c.get("domain")), location=loc, is_remote="remote" in (loc or "").lower(),
            description=desc, jd_emails=extract_emails(desc), posted_at=_as_date(j.get("updated_at")),
        ))
    return out


def _lever(c: dict) -> list[RawJob]:
    data = httpx.get(f"https://api.lever.co/v0/postings/{c['slug']}", params={"mode": "json"}, timeout=30).json()
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        desc = "\n\n".join(filter(None, [
            j.get("descriptionPlain"),
            *[f"{l.get('text')}\n{_strip_html(l.get('content'))}" for l in j.get("lists") or []],
            j.get("additionalPlain"),
        ]))
        created = j.get("createdAt")
        out.append(RawJob(
            source="lever", external_id=j["id"], url=j["hostedUrl"], apply_url=j.get("applyUrl"), title=j["text"],
            company=c["name"], company_domain=clean_domain(c.get("domain")), location=loc,
            is_remote=j.get("workplaceType") == "remote" or "remote" in (loc or "").lower(),
            description=desc, jd_emails=extract_emails(desc),
            posted_at=datetime.fromtimestamp(created / 1000, tz=timezone.utc).date() if created else None,
        ))
    return out


def _ashby(c: dict) -> list[RawJob]:
    data = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}", timeout=30).json()
    out = []
    for j in data.get("jobs", []):
        loc = j.get("location")
        desc = j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml"))
        out.append(RawJob(
            source="ashby", external_id=j.get("id"), url=j.get("jobUrl"), apply_url=j.get("applyUrl"), title=j["title"],
            company=c["name"], company_domain=clean_domain(c.get("domain")), location=loc,
            is_remote=bool(j.get("isRemote")), description=desc, jd_emails=extract_emails(desc),
            posted_at=_as_date(j.get("publishedAt")),
        ))
    return out


ATS_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def load_ats_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        return []
    return json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))


def fetch_ats(company: dict, freshness_days: int) -> list[RawJob]:
    fetcher = ATS_FETCHERS[company["ats"]]
    cutoff = date.today() - timedelta(days=freshness_days)
    return [
        j for j in fetcher(company)
        if (j.is_remote or _looks_india(j.location)) and (j.posted_at is None or j.posted_at >= cutoff)
    ]
