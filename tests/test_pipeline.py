"""End-to-end run against SQLite with every external service faked."""

import pytest
from sqlalchemy import select

from app.db import Base, engine, session_scope
from app.models import Draft, Job, JobStatus, Run
from app.pipeline import runner
from app.pipeline.fetch import RawJob
from app.providers.finders import Candidate
from app.providers.llm import ProviderExhausted

DESC = "Build LLM agents with FastAPI and LangGraph. 1-3 years experience. " * 8


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


class FakeProvider:
    def __init__(self, name, fail_after=None):
        self.name, self.fail_after, self.calls, self.exhausted, self.api_key = name, fail_after, 0, False, "k"

    @property
    def enabled(self):
        return not self.exhausted

    def complete_json(self, system, user):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            self.exhausted = True
            raise ProviderExhausted(f"{self.name} out of quota")
        if "Return ONLY JSON:\n{\n  \"score\"" in system:
            score = 40 if "Data Analyst" in user else 85
            return {"score": score, "summary": "s", "fit_points": ["FastAPI -> built FastAPI agents"], "gaps": [],
                    "company_domain": "acme.in"}
        return {"subject": "AI Engineer role - LLM agents in prod", "body": "word " * 100}


class FakeGmail:
    saved = []

    def __init__(self, *a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def save_draft(self, msg):
        FakeGmail.saved.append(msg)


class FakeFinder:
    def __init__(self):
        self.enabled, self.exhausted = True, False


def make_jobs():
    return [
        RawJob(source="naukri", url=f"https://n/{i}", title="AI Engineer", company=f"Company{i}",
               location="Mumbai, India", description=DESC, jd_emails=[f"hr@company{i}.in"] if i % 2 == 0 else [])
        for i in range(6)
    ] + [
        RawJob(source="naukri", url="https://n/sr", title="Senior AI Engineer", company="Big", location="Pune", description=DESC),
        RawJob(source="naukri", url="https://n/da", title="Python Developer Data Analyst", company="DA Co",
               location="Pune", description=DESC),
    ]


@pytest.fixture
def wired(monkeypatch):
    FakeGmail.saved = []
    s = runner.get_settings()
    monkeypatch.setattr(s, "job_sites", ["naukri"])
    monkeypatch.setattr(s, "search_terms", ["AI Engineer"])
    monkeypatch.setattr(s, "daily_draft_target", 3)
    monkeypatch.setattr(runner, "fetch_jobspy", lambda settings, site, term: make_jobs())
    monkeypatch.setattr(runner, "load_ats_companies", lambda: [])
    monkeypatch.setattr(runner, "get_resume_text", lambda url: "RESUME TEXT")
    monkeypatch.setattr(runner, "GmailDrafts", FakeGmail)
    monkeypatch.setattr(runner, "finders_from_settings", lambda: (FakeFinder(), FakeFinder()))

    def fake_find(job, company, apollo, hunter, log):
        if job.jd_emails:
            return Candidate(source="jd", email=job.jd_emails[0], confidence=95)
        return Candidate(source="hunter", email=f"priya@{job.company.lower()}.in", first_name="Priya", title="Recruiter")

    monkeypatch.setattr(runner, "find_contact", fake_find)
    return s


def new_run(providers):
    run_id = runner.start_run("manual")
    p = runner.PipelineRun(run_id)
    p.llm.providers = providers
    return run_id, p


def test_full_run_respects_daily_target(wired):
    run_id, p = new_run([FakeProvider("gemini"), FakeProvider("groq")])
    p.execute()

    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run.status == "success", run.error
        statuses = {j.title + j.company: j.status for j in s.scalars(select(Job))}
        drafts = list(s.scalars(select(Draft)))

    assert statuses["Senior AI EngineerBig"] == JobStatus.FILTERED_OUT
    assert statuses["Python Developer Data AnalystDA Co"] == JobStatus.LOW_SCORE
    assert len(drafts) == 3 == len(FakeGmail.saved)
    assert sum(1 for v in statuses.values() if v == JobStatus.SCORED) == 3  # waiting for tomorrow
    body = drafts[0].body
    assert body.startswith("Hi ") and "Resume: https://drive.google.com" in body and "Job posting: https://n/" in body


def test_second_run_same_day_does_not_exceed_target_or_redo_jobs(wired):
    _, p = new_run([FakeProvider("gemini")])
    p.execute()
    run_id, p2 = new_run([FakeProvider("gemini")])
    p2.execute()
    with session_scope() as s:
        assert len(list(s.scalars(select(Draft)))) == 3
        assert s.get(Run, run_id).stats.get("new_jobs", 0) == 0


def test_falls_back_to_groq_when_gemini_runs_out(wired):
    gemini, groq = FakeProvider("gemini", fail_after=2), FakeProvider("groq")
    run_id, p = new_run([gemini, groq])
    p.execute()
    with session_scope() as s:
        assert s.get(Run, run_id).status == "success"
        providers = {d.llm_provider for d in s.scalars(select(Draft))}
    assert groq.calls > 0 and providers == {"groq"}


def test_all_llms_exhausted_leaves_jobs_for_next_run(wired):
    run_id, p = new_run([FakeProvider("gemini", fail_after=1), FakeProvider("groq", fail_after=1)])
    p.execute()
    with session_scope() as s:
        run = s.get(Run, run_id)
        new_left = s.scalars(select(Job).where(Job.status == JobStatus.NEW)).all()
    assert run.status == "partial"
    assert len(new_left) >= 3


def test_only_one_run_at_a_time(wired):
    assert runner.start_run("manual") is not None
    assert runner.start_run("cron") is None


def test_dashboard_endpoints_after_a_run(wired):
    from fastapi.testclient import TestClient

    from app.main import app

    _, p = new_run([FakeProvider("gemini")])
    p.execute()
    h = {"X-API-Key": "test-key"}
    with TestClient(app) as c:
        drafts = c.get("/drafts?status=drafted", headers=h).json()
        assert len(drafts) == 3
        d = drafts[0]
        assert d["job"]["company"] and d["to_email"] and d["contact_source"] in {"jd", "hunter"}
        assert c.patch(f"/drafts/{d['id']}", json={"status": "replied"}, headers=h).json()["status"] == "replied"
        stats = c.get("/stats", headers=h).json()
        assert stats["drafts_by_status"]["replied"] == 1 and stats["reply_rate"] == 100.0
        assert stats["jobs_by_status"]["scored"] == 3 and stats["last_run"]["status"] == "success"
        job_id = c.get("/jobs?status=low_score", headers=h).json()[0]["id"]
        assert c.patch(f"/jobs/{job_id}", json={"status": "scored"}, headers=h).json()["status"] == "scored"
