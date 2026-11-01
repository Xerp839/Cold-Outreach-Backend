"""End-to-end run against SQLite with every external service faked."""

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Draft, Event, Job, JobStatus, LinkedInNote, Run
from app.pipeline import runner
from app.pipeline.fetch import RawJob
from app.providers.finders import Apollo, Candidate, Hunter
from app.providers.llm import ProviderError, ProviderExhausted

DESC = "Build LLM agents with FastAPI and LangGraph. 1-3 years experience. " * 8


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
        if "connection-request notes" in system:
            return {"note": "Saw the AI Engineer opening - I build LLM agents in production at Quantal. Would love to connect."}
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
               location="Mumbai, India", description=DESC, jd_emails=[f"hr@company{i}.in"] if i % 2 == 0 else [],
               company_linkedin_url="https://www.linkedin.com/company/company0" if i == 0 else None)
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
    monkeypatch.setattr(runner, "fetch_jobspy", lambda settings, site, term, problems=None: make_jobs())
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


class ScoringBroken(FakeProvider):
    def complete_json(self, system, user):
        if '"score"' in system:
            raise ProviderError(f"{self.name} bad response")
        return super().complete_json(system, user)


def test_scoring_failures_make_the_run_partial_and_retry_next_run(wired):
    run_id, p = new_run([ScoringBroken("gemini")])
    p.execute()
    with session_scope() as s:
        run = s.get(Run, run_id)
        statuses = {j.status for j in s.scalars(select(Job).where(Job.status != JobStatus.FILTERED_OUT))}
    assert run.status == "partial" and run.stats.get("scored", 0) == 0
    assert statuses == {JobStatus.NEW}

    run_id, p = new_run([FakeProvider("gemini")])
    p.execute()
    with session_scope() as s:
        assert s.get(Run, run_id).stats["scored"] >= 5


def test_jobs_errored_by_old_scoring_are_requeued(wired):
    _, p = new_run([FakeProvider("gemini")])
    p.fetch_stage()
    with session_scope() as s:
        for job in s.scalars(select(Job).where(Job.status == JobStatus.NEW)):
            job.status, job.status_reason = JobStatus.ERROR, "scoring failed: gemini HTTP 404"
    p.score_stage()
    with session_scope() as s:
        assert not s.scalars(select(Job).where(Job.status == JobStatus.ERROR)).all()
    assert p.stats["scored"] >= 5


def test_jobs_wait_for_tomorrow_when_hunter_share_is_spent(wired, monkeypatch):
    monkeypatch.setattr(runner, "finders_from_settings", lambda: (Apollo(""), Hunter("k", daily_left=0)))
    _, p = new_run([FakeProvider("gemini")])
    p.execute()
    with session_scope() as s:
        drafts = list(s.scalars(select(Draft)))
        waiting = s.scalars(select(Job).where(Job.status_reason == "waiting for tomorrow's Hunter searches")).all()
        assert all(j.status == JobStatus.SCORED and not j.jd_emails for j in waiting)
    assert len(drafts) == 3  # the three jobs with an email in the JD don't need Hunter
    assert waiting and p.stats["no_contact"] == 0  # queued, not dumped into "apply manually"


def test_linkedin_notes_for_every_good_fit_company(wired, monkeypatch):
    monkeypatch.setattr(wired, "linkedin_daily_notes", 4)
    run_id, p = new_run([FakeProvider("gemini")])
    p.execute()
    with session_scope() as s:
        notes = list(s.scalars(select(LinkedInNote)))
        noted_statuses = {n.job.status for n in notes}
        pages = {n.job.company: n.company_linkedin_url for n in notes}
    assert len(notes) == 4 == p.stats["linkedin_notes"]  # 6 good fits, capped at today's 4
    assert noted_statuses == {JobStatus.DRAFTED, JobStatus.SCORED}  # emailed jobs get one too
    assert pages["Company0"] == "https://www.linkedin.com/company/company0"

    _, p2 = new_run([FakeProvider("gemini")])
    p2.execute()
    with session_scope() as s:
        assert len(list(s.scalars(select(LinkedInNote)))) == 4  # today's quota already used


def test_blocked_source_is_reported(wired, monkeypatch):
    def fake_fetch(settings, site, term, problems=None):
        problems.append("response status code 429 (blocked: use a proxy)")
        return []

    monkeypatch.setattr(wired, "job_sites", ["linkedin"])
    monkeypatch.setattr(runner, "fetch_jobspy", fake_fetch)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    run_id, p = new_run([FakeProvider("gemini")])
    p.execute()
    with session_scope() as s:
        run = s.get(Run, run_id)
        events = [e.message for e in s.scalars(select(Event).where(Event.run_id == run_id))]
    assert run.status == "partial" and run.stats["fetched_linkedin"] == 0 and run.stats["blocked_linkedin"] == 1
    assert any("linkedin: 0 jobs" in m and "blocking this server" in m for m in events)
    assert any("429" in m for m in events)


def test_interrupted_run_is_failed_on_startup(wired):
    run_id = runner.start_run("cron")
    runner.log_event(run_id, "score", "info", "scoring")
    runner.fail_interrupted_runs()
    with session_scope() as s:
        run = s.get(Run, run_id)
    assert run.status == "failed" and "during score" in run.error
    assert runner.start_run("manual") is not None


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
        notes = c.get("/linkedin?status=pending", headers=h).json()
        assert notes and notes[0]["note"] and notes[0]["people_url"].startswith("https://www.linkedin.com/")
        assert notes[0]["max_chars"] == 300
        assert c.patch(f"/linkedin/{notes[0]['id']}", json={"status": "sent"}, headers=h).json()["status"] == "sent"
        assert c.get("/stats", headers=h).json()["linkedin_by_status"]["sent"] == 1
        job_id = c.get("/jobs?status=low_score", headers=h).json()[0]["id"]
        assert c.patch(f"/jobs/{job_id}", json={"status": "scored"}, headers=h).json()["status"] == "scored"
