"""LinkedIn connection-request notes. The pipeline writes them; I find the person on LinkedIn and send them by hand.

The note is stored without a greeting: the dashboard adds "Hi <name>, " once I know who it's going to,
so the body leaves room for that inside LinkedIn's character limit.
"""

import re
from urllib.parse import quote

from app.config import Settings
from app.pipeline.scoring import build_candidate_block
from app.providers.llm import LLMChain, ProviderError

GREETING_ROOM = 25  # "Hi Firstname, " with a long first name

NOTE_SYSTEM = """You write LinkedIn connection-request notes from a job seeker in India. The reader works at the
hiring company (recruiter, hiring manager or engineer). Their name is added separately.

Body only - NO greeting, NO sign-off, NO links, NO emojis, NO hashtags.
Hard limit: {limit} characters including spaces. Aim for {low}-{limit}.

Content, in 2-3 short sentences:
1. The exact role at the company you're reaching out about.
2. ONE concrete achievement from the resume that matches what this job needs (keep the number if the resume has one),
   with current role + years of experience.
3. A light ask: connect, and happy to share more / would value a pointer to the right person.

Sound like a message to a peer, not a cover letter: plain words, contractions are fine. Open with the role,
e.g. "Saw the <role> opening at <company> -". Never start with "I am reaching out" or "I came across".
Never invent experience, numbers or skills. No "I hope you're doing well", "passionate", "synergy", "perfect fit".
Return ONLY JSON: {{"note": "..."}}"""

_PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,40}\]|\{[^}]{2,40}\}")


def note_limit(settings: Settings) -> int:
    return settings.linkedin_note_max_chars - GREETING_ROOM


def _validate(note: str, limit: int) -> str | None:
    if not note:
        return "empty note"
    if len(note) > limit:
        return f"note is {len(note)} characters, the hard limit is {limit}"
    if _PLACEHOLDER_RE.search(note):
        return "left a placeholder in the text"
    if "http" in note or "www." in note:
        return "contains a link"
    return None


def write_note(llm: LLMChain, settings: Settings, resume_text: str, job) -> tuple[str, str]:
    """Returns (note, provider)."""
    limit = note_limit(settings)
    fit = "\n".join(f"- {p}" for p in (job.score_details or {}).get("fit_points", []))
    system = NOTE_SYSTEM.format(limit=limit, low=limit - 80)
    user = (
        f"{build_candidate_block(settings, resume_text)}\n\n---\n"
        f"JOB: {job.title} at {job.company} ({job.location or 'location n/a'}{', remote' if job.is_remote else ''})\n"
        f"Fit points from screening:\n{fit or '- (none)'}\n\n"
        f"JOB DESCRIPTION:\n{(job.description or '')[:3000]}"
    )
    problem = None
    for _ in range(2):
        prompt = user if not problem else f"{user}\n\nYour previous attempt was rejected: {problem}. Fix it."
        data, provider = llm.complete_json(system, prompt)
        note = " ".join(str(data.get("note") or "").split()).strip('"')
        problem = _validate(note, limit)
        if problem is None:
            return note, provider
    raise ProviderError(f"LinkedIn note failed validation twice: {problem}")


def company_links(company: str, page_url: str | None) -> dict:
    """Company page + a people search for recruiters there. Falls back to LinkedIn search when the job didn't
    come from LinkedIn and we don't know the page."""
    if page_url:
        page = page_url.rstrip("/")
        return {"company_url": page, "people_url": f"{page}/people/?keywords=recruiter", "company_page_known": True}
    return {
        "company_url": f"https://www.linkedin.com/search/results/companies/?keywords={quote(company)}",
        "people_url": f"https://www.linkedin.com/search/results/people/?keywords={quote(company + ' recruiter')}",
        "company_page_known": False,
    }
