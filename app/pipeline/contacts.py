"""Finds one good person to email per job, cheapest source first:

1. Emails printed in the job description (free, and the poster asked for them).
2. Apollo people search -> reveal one email (1 credit).
3. Hunter domain search (1 search credit) -> best matching person, else a careers@/hr@ inbox, and the email pattern.
4. Apollo name + Hunter pattern -> constructed email (low confidence, flagged in the dashboard).

Results are cached per company so credits are never spent twice on the same company.
"""

import re
from typing import Callable

from app.emails import apply_pattern, clean_domain, first_name_from_email, has_mx
from app.models import Company, Job, utcnow
from app.providers.finders import TARGET_TITLES, Apollo, Candidate, Hunter
from app.providers.llm import ProviderError, ProviderExhausted

# A named person beats a shared careers@ inbox, but the inbox beats no contact at all.
_TITLE_PATTERNS = [re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])") for t in TARGET_TITLES + ["careers inbox"]]


def title_rank(title: str | None) -> int | None:
    """Lower is better. None = not someone worth emailing about a job."""
    if not title:
        return None
    t = title.lower()
    for i, p in enumerate(_TITLE_PATTERNS):
        if p.search(t):
            return i
    return None


def rank_candidates(cands: list[Candidate]) -> list[Candidate]:
    ranked = [(title_rank(c.title), c) for c in cands]
    ranked = [(r, c) for r, c in ranked if r is not None]
    ranked.sort(key=lambda rc: (rc[0], -(rc[1].confidence or 0)))
    return [c for _, c in ranked]


def resolve_domain(job: Job, company: Company, log: Callable[[str, str], None]) -> str | None:
    if company.domain and company.domain_verified:
        return company.domain
    guesses = [job.company_domain, company.domain, (job.score_details or {}).get("company_domain")]
    for guess in guesses:
        domain = clean_domain(guess) if guess else None
        if domain and has_mx(domain):
            company.domain, company.domain_verified = domain, True
            return domain
    log("warning", f"no mail-accepting domain found for {job.company} (guesses: {[g for g in guesses if g]})")
    return None


def find_contact(job: Job, company: Company, apollo: Apollo, hunter: Hunter, log: Callable[[str, str], None]) -> Candidate | None:
    # 1. Emails in the JD
    if job.jd_emails:
        email = job.jd_emails[0]
        return Candidate(source="jd", email=email, first_name=first_name_from_email(email), confidence=95, title="from job post")

    domain = resolve_domain(job, company, log)
    if not domain:
        return None

    apollo_people: list[Candidate] = []

    # 2. Apollo
    if apollo.enabled and company.apollo_checked_at is None:
        try:
            apollo_people = rank_candidates(apollo.search_people(domain))
            company.apollo_checked_at = utcnow()
            for cand in apollo_people[:2]:
                apollo.reveal(cand)
                if cand.email:
                    return cand
        except ProviderExhausted as e:
            log("warning", f"Apollo exhausted, falling back to Hunter: {e}")
        except ProviderError as e:
            log("warning", f"Apollo error for {domain}: {e}")

    # 3. Hunter
    if hunter.enabled and company.hunter_checked_at is None:
        try:
            people, pattern = hunter.domain_search(domain)
            company.hunter_checked_at = utcnow()
            company.email_pattern = pattern or company.email_pattern
            ranked = rank_candidates(people)
            if ranked:
                return ranked[0]
        except ProviderExhausted as e:
            log("warning", f"Hunter exhausted: {e}")
        except ProviderError as e:
            log("warning", f"Hunter error for {domain}: {e}")

    # 4. Pattern + known name
    if company.email_pattern:
        for cand in apollo_people:
            email = apply_pattern(company.email_pattern, cand.first_name, cand.last_name, domain)
            if email:
                cand.email, cand.source, cand.confidence = email, "pattern", 40
                return cand
    return None
