from app.config import Settings
from app.providers.llm import LLMChain

SCORING_SYSTEM = """You are a blunt, experienced tech recruiter in India screening jobs FOR a candidate.
Judge how likely this candidate is to get an interview if they reach out, not how cool the job is.

Scoring rubric (0-100):
- 85-100: core stack + seniority match; candidate's past work maps directly to the main responsibilities.
- 70-84: strong overlap, one or two learnable gaps.
- 50-69: partial overlap or seniority stretch.
- <50: different stack, too senior, wrong role type, or clearly outside the candidate's salary band.
Penalise hard: roles needing far more experience than the candidate has, non-engineering roles,
staffing/body-shop postings with no real company, unpaid/internship roles.

Return ONLY JSON:
{
  "score": int,
  "summary": "one line: why this score",
  "fit_points": ["3-4 items. Each pairs a SPECIFIC thing the JD asks for with a SPECIFIC achievement from the resume, including numbers when the resume has them. Never invent experience."],
  "gaps": ["0-3 honest gaps"],
  "company_domain": "the company's primary website domain, e.g. 'razorpay.com', or null if you are not confident"
}"""


def build_candidate_block(settings: Settings, resume_text: str) -> str:
    return (
        f"CANDIDATE: {settings.candidate_name}, {settings.candidate_current_role}"
        f"{' at ' + settings.candidate_current_company if settings.candidate_current_company else ''}, "
        f"~{settings.candidate_years_experience} years experience, based in {settings.candidate_location}.\n"
        f"Preferences: {settings.candidate_notes}\n\nRESUME:\n{resume_text}"
    )


def score_job(llm: LLMChain, settings: Settings, resume_text: str, job) -> dict:
    user = (
        f"{build_candidate_block(settings, resume_text)}\n\n"
        f"---\nJOB: {job.title} at {job.company}\nLocation: {job.location or 'n/a'}"
        f"{' (remote)' if job.is_remote else ''}\n"
        f"Experience asked: {job.experience_text or 'see description'}\nSalary: {job.salary_text or 'not disclosed'}\n\n"
        f"DESCRIPTION:\n{(job.description or '')[:7000]}"
    )
    data, provider = llm.complete_json(SCORING_SYSTEM, user)
    try:
        score = max(0, min(100, int(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    return {
        "score": score,
        "summary": str(data.get("summary") or "")[:500],
        "fit_points": [str(p) for p in (data.get("fit_points") or [])][:4],
        "gaps": [str(g) for g in (data.get("gaps") or [])][:3],
        "company_domain": data.get("company_domain"),
        "provider": provider,
    }
