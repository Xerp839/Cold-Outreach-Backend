"""Writes the email. The LLM writes the body only; links and signature are appended in code so they're never mangled."""

import re

from app.config import Settings
from app.providers.llm import LLMChain, ProviderError
from app.pipeline.scoring import build_candidate_block

WRITER_SYSTEM = """You write short cold emails from a job seeker to a recruiter / hiring manager in India.
The reader gets dozens of these. Yours must be specific enough that it's obviously not a template.

Structure (body only - NO greeting line, NO sign-off, NO links; those are added separately):
1. One line: who the candidate is (current role + company + years) and the exact role they're reaching out about.
2. "Why I'm a fit:" then 3 bullet points ("- "). Each bullet ties something THIS job asks for to a concrete
   achievement from the resume, with the number if the resume has one. Use the provided fit points as raw material.
3. One line call to action: ask for a quick chat or to be referred to the right person. Mention openness to
   relocation/remote only if the job is not in the candidate's city.

Rules:
- 90-140 words for the body. Plain text. No emojis, no buzzwords ("passionate", "synergy", "leverage"), no "I hope this finds you well".
- Never invent experience, numbers, or skills that are not in the resume.
- Confident and direct, not desperate. Do not say "perfect fit".
- Subject: under 9 words, concrete, includes the role name. Example: "AI Engineer role - built LLM agents in production".

Return ONLY JSON: {"subject": "...", "body": "..."}"""

_PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,40}\]|\{[^}]{2,40}\}")


def build_signature(settings: Settings) -> str:
    lines = ["", f"Portfolio: {settings.candidate_portfolio}" if settings.candidate_portfolio else None,
             f"Resume: {settings.resume_url}", "", "Regards,", settings.candidate_name]
    contact = " | ".join(p for p in (settings.candidate_phone, settings.candidate_linkedin) if p)
    if contact:
        lines.append(contact)
    return "\n".join(l for l in lines if l is not None)


def greeting(first_name: str | None) -> str:
    return f"Hi {first_name}," if first_name else "Hi Hiring Team,"


def _validate(subject: str, body: str) -> str | None:
    if not subject or not body:
        return "empty subject/body"
    if _PLACEHOLDER_RE.search(subject + body):
        return "left a placeholder in the text"
    words = len(body.split())
    if words > 200 or words < 50:
        return f"body is {words} words"
    return None


def write_email(llm: LLMChain, settings: Settings, resume_text: str, job, contact_first_name: str | None,
                contact_title: str | None) -> tuple[str, str, str]:
    """Returns (subject, full_body, provider)."""
    details = job.score_details or {}
    fit = "\n".join(f"- {p}" for p in details.get("fit_points", []))
    user = (
        f"{build_candidate_block(settings, resume_text)}\n\n---\n"
        f"JOB: {job.title} at {job.company} ({job.location or 'location n/a'}{', remote' if job.is_remote else ''})\n"
        f"Job link: {job.url}\n"
        f"Writing to: {contact_first_name or 'the hiring team'}{f' ({contact_title})' if contact_title else ''}\n"
        f"Fit points from screening:\n{fit or '- (none)'}\n\n"
        f"JOB DESCRIPTION:\n{(job.description or '')[:5000]}"
    )
    last_problem = None
    for _ in range(2):
        prompt = user if not last_problem else f"{user}\n\nYour previous attempt was rejected: {last_problem}. Fix it."
        data, provider = llm.complete_json(WRITER_SYSTEM, prompt)
        subject = str(data.get("subject") or "").strip().strip('"')
        body = str(data.get("body") or "").strip()
        last_problem = _validate(subject, body)
        if last_problem is None:
            full = f"{greeting(contact_first_name)}\n\n{body}\n\nJob posting: {job.url}\n{build_signature(settings)}"
            return subject, full, provider
    raise ProviderError(f"email draft failed validation twice: {last_problem}")
