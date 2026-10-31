from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    database_url: str
    api_key: str  # shared secret for the dashboard + GitHub Actions cron
    frontend_origins: CsvList = ["http://localhost:5173"]

    # --- Candidate profile (kept out of code so the repo can stay public) ---
    candidate_name: str
    candidate_phone: str = ""
    candidate_linkedin: str = ""
    candidate_portfolio: str = ""
    candidate_github: str = ""
    resume_url: str  # public Google Drive share link, used in emails AND to read the resume text
    candidate_current_role: str = "AI Engineer"
    candidate_current_company: str = ""
    candidate_years_experience: float = 1.3
    candidate_location: str = "Mumbai"
    candidate_notes: str = "Open to relocation anywhere in India and to remote roles. Targeting 8-10 LPA."

    # --- Gmail (IMAP APPEND into Drafts) ---
    gmail_address: str
    gmail_app_password: str

    # --- LLMs ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_rpm: int = 8
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_rpm: int = 25

    # --- Contact finders ---
    apollo_api_key: str = ""
    hunter_api_key: str = ""
    hunter_daily_searches: int = 2  # free plan: 60 searches/month -> spread over 30 days

    # --- Job search ---
    # Stack keywords (LangChain, FastAPI) surface far more junior-friendly matches than role titles alone,
    # especially on Naukri whose title search skews senior.
    search_terms: CsvList = [
        "AI Engineer",
        "LLM Engineer",
        "Generative AI Engineer",
        "LangChain",
        "FastAPI",
        "Python Developer",
    ]
    search_location: str = "India"
    job_sites: CsvList = ["linkedin", "naukri", "indeed"]
    results_per_search: int = 20
    hours_old: int = 72
    title_include: CsvList = [
        "ai", "ml", "llm", "genai", "gen ai", "generative", "machine learning", "artificial intelligence", "nlp",
        "python", "backend", "back end", "back-end", "software engineer", "sde", "software developer",
        "agent", "applied",
    ]
    title_exclude: CsvList = [
        "senior", "sr", "lead", "principal", "staff", "manager", "head", "director", "architect",
        "intern", "trainee", "vp", "vice president", "java", ".net", "php", "android", "ios",
        "frontend", "front end", "sales", "tester", "qa", "teacher", "trainer", "annotator",
    ]
    max_required_experience: int = 3  # drop jobs whose minimum ask is above this
    min_ctc_lpa: float = 6.0  # drop jobs that explicitly pay below this
    company_blocklist: CsvList = []

    # --- Pipeline limits (quality over quantity) ---
    daily_draft_target: int = 5  # what free Apollo + Hunter credits sustain over a month
    min_score: int = 70
    max_jobs_to_score: int = 40
    max_contact_lookups: int = 8
    company_cooldown_days: int = 30
    job_freshness_days: int = 7

    @field_validator(
        "frontend_origins", "search_terms", "job_sites", "title_include", "title_exclude", "company_blocklist",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
