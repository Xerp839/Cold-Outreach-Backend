import pytest

from app.config import get_settings
from app.emails import apply_pattern, domain_from_url, extract_emails, first_name_from_email
from app.gmail import build_message
from app.pipeline.contacts import rank_candidates, title_rank
from app.pipeline.fetch import RawJob
from app.pipeline.filters import min_required_years, rule_filter
from app.pipeline.resume import drive_download_url
from app.pipeline.runner import company_key
from app.pipeline.writer import _validate, build_signature, greeting
from app.providers.finders import Candidate
from app.providers.llm import ProviderError, parse_json

DESC = "We are building LLM agents with FastAPI and LangGraph. " * 10


def job(**kw) -> RawJob:
    base = dict(source="linkedin", url="https://x", title="AI Engineer", company="Acme", location="Mumbai, India",
                description=DESC)
    return RawJob(**{**base, **kw})


# ---- filters


@pytest.mark.parametrize("title,ok", [
    ("AI Engineer", True),
    ("Python Developer - GenAI", True),
    ("Software Engineer (Backend)", True),
    ("Senior AI Engineer", False),
    ("Sr. Python Developer", False),
    ("ML Engineer Intern", False),
    ("Java Backend Developer", False),
    ("Maintenance Supervisor", False),  # 'ai' inside a word must not match
    ("Chair Designer", False),
])
def test_title_filter(title, ok):
    assert (rule_filter(job(title=title), get_settings()) is None) is ok


def test_experience_parsing():
    assert min_required_years("2-5 Yrs", None) == 2
    assert min_required_years(None, "Requires 5+ years of experience in Python") == 5
    assert min_required_years(None, "1-3 years of hands-on experience") == 1
    assert min_required_years(None, "Founded 15 years ago. We like Python.") is None


def test_experience_too_high_rejected():
    assert "years" in rule_filter(job(experience_text="5-8 Yrs"), get_settings())
    assert rule_filter(job(experience_text="0-3 Yrs"), get_settings()) is None


def test_blocklist_salary_location():
    s = get_settings()
    assert "blocklisted" in rule_filter(job(company="Quantal Technologies"), s)
    assert "LPA" in rule_filter(job(salary_max_lpa=4.0), s)
    assert "outside India" in rule_filter(job(location="Berlin, Germany"), s)
    assert rule_filter(job(location="Berlin, Germany", is_remote=True), s) is None


# ---- emails


def test_extract_emails_skips_junk():
    text = "Send CV to hr@acme.in or Priya.Sharma@acme.in. Not noreply@acme.in."
    assert extract_emails(text) == ["hr@acme.in", "priya.sharma@acme.in"]


def test_domain_from_url():
    assert domain_from_url("https://www.razorpay.com/careers") == "razorpay.com"
    assert domain_from_url("https://www.linkedin.com/company/x") is None
    assert domain_from_url("https://wd1.myworkdaysite.com/x") is None


def test_apply_pattern():
    assert apply_pattern("{first}.{last}", "Priya", "Sharma", "acme.in") == "priya.sharma@acme.in"
    assert apply_pattern("{f}{last}", "Priya", "Sharma", "acme.in") == "psharma@acme.in"
    assert apply_pattern("{first}", "Priya", None, "acme.in") == "priya@acme.in"
    assert apply_pattern("{first}.{last}", "Priya", None, "acme.in") is None


def test_first_name_from_email():
    assert first_name_from_email("priya.sharma@acme.in") == "Priya"
    assert first_name_from_email("careers@acme.in") is None
    assert first_name_from_email("hr.team@acme.in") is None


# ---- contacts


def test_title_rank_prefers_recruiters():
    assert title_rank("Senior Technical Recruiter") < title_rank("Engineering Manager")
    assert title_rank("Sales Executive") is None
    assert title_rank("Chrome Developer") is None  # 'hr' must not match inside words
    ranked = rank_candidates([
        Candidate(source="hunter", title="Founder"),
        Candidate(source="hunter", title="Talent Acquisition Partner"),
        Candidate(source="hunter", title="Marketing Lead"),
    ])
    assert [c.title for c in ranked] == ["Talent Acquisition Partner", "Founder"]


def test_company_key_merges_variants():
    assert company_key("Acme Technologies Pvt. Ltd.") == company_key("Acme")


# ---- writer


def test_signature_and_greeting():
    sig = build_signature(get_settings())
    assert "Resume: https://drive.google.com" in sig and "Portfolio: https://test.netlify.app" in sig
    assert sig.strip().splitlines()[-1].startswith("+91")
    assert greeting(None) == "Hi Hiring Team,"
    assert greeting("Priya") == "Hi Priya,"


def test_validate_catches_placeholders_and_length():
    body = "word " * 100
    assert _validate("AI Engineer role", body) is None
    assert "placeholder" in _validate("AI Engineer role", body + " [your name]")
    assert "words" in _validate("AI Engineer role", "too short")


# ---- misc


def test_parse_json_handles_fences():
    assert parse_json('```json\n{"score": 80}\n```') == {"score": 80}
    with pytest.raises(ProviderError):
        parse_json("no json here")


def test_drive_download_url():
    assert drive_download_url("https://drive.google.com/file/d/1tWL-x_Y/view?usp=sharing") == \
        "https://drive.google.com/uc?export=download&id=1tWL-x_Y"


def test_build_message():
    msg = build_message("me@gmail.com", "Test Candidate", "priya@acme.in", "Priya Sharma", "Subject", "Body")
    assert msg["To"] == "Priya Sharma <priya@acme.in>"
    assert msg["From"] == "Test Candidate <me@gmail.com>"
    assert msg.get_content().strip() == "Body"
