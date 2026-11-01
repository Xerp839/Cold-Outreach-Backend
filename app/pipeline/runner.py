"""One pipeline run: fetch -> rule filter -> LLM score -> find contact -> write -> Gmail draft -> LinkedIn notes.

Every stage commits per item, so a crash or quota wall halfway through keeps the work already done,
and the next run picks up where this one stopped.
"""

import os
import re
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select, update

from app.config import Settings, get_settings
from app.db import session_scope
from app.gmail import GmailDrafts, build_message
from app.models import Company, Contact, Draft, Event, Job, JobStatus, LinkedInNote, Run, as_aware, utcnow
from app.pipeline.contacts import find_contact
from app.pipeline.fetch import RawJob, fetch_ats, fetch_jobspy, load_ats_companies
from app.pipeline.filters import rule_filter
from app.pipeline.linkedin import write_note
from app.pipeline.resume import get_resume_text
from app.pipeline.scoring import score_job
from app.pipeline.writer import write_email
from app.providers.finders import finders_from_settings
from app.providers.llm import AllProvidersFailed, LLMChain, ProviderError
from app.tracking import log_event

IST = timezone(timedelta(hours=5, minutes=30))
STALE_RUN_AFTER = timedelta(hours=3)


def ist_day_start() -> datetime:
    now = datetime.now(IST)
    # In UTC: SQLite compares timestamps as text, so the offset has to match how they're stored.
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def company_key(name: str) -> str:
    name = re.sub(r"\b(pvt|private|ltd|limited|llp|inc|technologies|technology|solutions|labs)\b\.?", "", name.lower())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()[:300] or name.lower()[:300]


# --------------------------------------------------------------------------- run lifecycle


def start_run(trigger: str) -> int | None:
    """Creates a run row. Returns None if one is already running."""
    with session_scope() as s:
        s.execute(
            update(Run)
            .where(Run.status == "running", Run.started_at < utcnow() - STALE_RUN_AFTER)
            .values(status="failed", finished_at=utcnow(), error="run died (server restart or sleep)")
        )
        if s.scalar(select(func.count()).select_from(Run).where(Run.status == "running")):
            return None
        run = Run(trigger=trigger, status="running", stats={})
        s.add(run)
        s.flush()
        return run.id


def fail_interrupted_runs() -> None:
    """Called when the server starts. There's only one instance, so a run still marked running died with the
    previous process (Render restarts it after running out of memory, on a deploy, or when it falls asleep)."""
    with session_scope() as s:
        for run in s.scalars(select(Run).where(Run.status == "running")):
            last = s.scalar(select(Event.stage).where(Event.run_id == run.id).order_by(Event.id.desc()).limit(1))
            run.status, run.finished_at = "failed", utcnow()
            run.error = (f"server restarted mid-run{f' during {last}' if last else ''} (out of memory, a deploy, "
                         "or the free instance went to sleep). Nothing is lost: the next run continues from here.")


class PipelineRun:
    def __init__(self, run_id: int, settings: Settings | None = None):
        self.run_id = run_id
        self.settings = settings or get_settings()
        self.stats: Counter = Counter()
        self.errors = 0
        self.llm = LLMChain.from_settings()
        self.apollo, self.hunter = finders_from_settings()
        self.resume_text = ""

    def log(self, stage: str, level: str, message: str, job_id: int | None = None) -> None:
        if level == "error":
            self.errors += 1
        log_event(self.run_id, stage, level, message, job_id)

    def save_stats(self, status: str | None = None, error: str | None = None) -> None:
        with session_scope() as s:
            run = s.get(Run, self.run_id)
            run.stats = dict(self.stats)
            if status:
                run.status = status
                run.finished_at = utcnow()
                run.error = error

    def execute(self) -> None:
        try:
            self.resume_text = get_resume_text(self.settings.resume_url)
            self.fetch_stage()
            self.save_stats()
            self.score_stage()
            self.save_stats()
            self.draft_stage()
            self.save_stats()
            self.linkedin_stage()
            self.save_stats(status="partial" if self.errors else "success")
            self.log("run", "info", f"finished: {dict(self.stats)}")
        except Exception as e:
            self.log("run", "error", f"run crashed: {e}\n{traceback.format_exc()[-1500:]}")
            self.save_stats(status="failed", error=str(e)[:2000])

    # ----------------------------------------------------------------------- fetch + filter

    def fetch_stage(self) -> None:
        raw: list[RawJob] = []
        for site in self.settings.job_sites:
            self.stats[f"fetched_{site}"] += 0  # so a site that returned nothing still shows up as 0
            for term in self.settings.search_terms:
                problems: list[str] = []
                try:
                    found = fetch_jobspy(self.settings, site, term, problems)
                    raw.extend(found)
                    self.stats[f"fetched_{site}"] += len(found)
                    if problems:
                        self.log("fetch", "warning", f"{site} '{term}': got {len(found)} jobs, {site} said: {'; '.join(problems)[:400]}")
                except Exception as e:
                    problems.append(f"{type(e).__name__}: {str(e)[:300]}")
                    self.log("fetch", "warning", f"{site} '{term}' failed: {problems[-1]}")
                if problems:
                    self.stats[f"blocked_{site}"] += 1
                    self.stats["fetch_failures"] += 1
                if site == "linkedin":
                    time.sleep(3)  # be gentle; LinkedIn rate limits aggressively
            if self.stats[f"fetched_{site}"] == 0:
                self.log("fetch", "error", f"{site}: 0 jobs from {len(self.settings.search_terms)} searches"
                         f"{' - it is blocking this server' if self.stats[f'blocked_{site}'] else ''}. "
                         f"If this repeats, drop it from JOB_SITES.")
        for company in load_ats_companies():
            try:
                found = fetch_ats(company, self.settings.job_freshness_days)
                raw.extend(found)
                self.stats[f"fetched_{company['ats']}"] += len(found)
            except Exception as e:
                self.log("fetch", "warning", f"{company.get('ats')}:{company.get('slug')} failed: {str(e)[:300]}")
                self.stats["fetch_failures"] += 1

        if not raw and self.stats["fetch_failures"]:
            self.log("fetch", "error", "every job source failed")

        unique: dict[str, RawJob] = {}
        for job in raw:
            unique.setdefault(job.dedupe_key, job)
        self.stats["fetched"] = len(raw)

        with session_scope() as s:
            keys = list(unique)
            existing = set()
            for i in range(0, len(keys), 500):
                existing |= set(s.scalars(select(Job.dedupe_key).where(Job.dedupe_key.in_(keys[i:i + 500]))))
            for key, rj in unique.items():
                if key in existing:
                    continue
                reason = rule_filter(rj, self.settings)
                s.add(Job(
                    dedupe_key=key, source=rj.source, external_id=rj.external_id, url=rj.url, apply_url=rj.apply_url,
                    title=rj.title[:300], company=rj.company[:300], company_domain=rj.company_domain,
                    company_linkedin_url=rj.company_linkedin_url,
                    location=(rj.location or "")[:300] or None, is_remote=rj.is_remote, description=rj.description,
                    experience_text=rj.experience_text, salary_text=rj.salary_text, jd_emails=rj.jd_emails or None,
                    posted_at=rj.posted_at, run_id=self.run_id,
                    status=JobStatus.FILTERED_OUT if reason else JobStatus.NEW, status_reason=reason,
                ))
                self.stats["new_jobs"] += 1
                self.stats["filtered_out" if reason else "passed_filter"] += 1

    # ----------------------------------------------------------------------- score

    def score_stage(self) -> None:
        cutoff = utcnow() - timedelta(days=self.settings.job_freshness_days)
        with session_scope() as s:
            # Older versions parked LLM failures as errors, where nothing ever retried them.
            s.execute(update(Job).where(Job.status == JobStatus.ERROR, Job.status_reason.like("scoring failed%"))
                      .values(status=JobStatus.NEW))
            ids = list(s.scalars(
                select(Job.id)
                .where(Job.status == JobStatus.NEW, Job.fetched_at >= cutoff)
                .order_by(Job.posted_at.desc().nulls_last(), Job.id.desc())
                .limit(self.settings.max_jobs_to_score)
            ))
        for job_id in ids:
            if not self.llm.any_available:
                self.log("score", "error", "all LLM providers exhausted - remaining jobs will be scored next run")
                break
            with session_scope() as s:
                job = s.get(Job, job_id)
                try:
                    result = score_job(self.llm, self.settings, self.resume_text, job)
                except AllProvidersFailed as e:
                    # The job stays new so the next run scores it; an error makes this run partial, not success.
                    job.status_reason = f"scoring failed: {str(e)[:500]}"
                    self.log("score", "error", f"scoring failed for #{job.id}: {e}", job.id)
                    continue
                job.score, job.score_details, job.scored_at = result["score"], result, utcnow()
                if result["score"] >= self.settings.min_score:
                    job.status, job.status_reason = JobStatus.SCORED, None
                    self.stats["good_fit"] += 1
                else:
                    job.status, job.status_reason = JobStatus.LOW_SCORE, result["summary"]
                self.stats["scored"] += 1

    # ----------------------------------------------------------------------- contacts + drafts

    def draft_stage(self) -> None:
        settings = self.settings
        with session_scope() as s:
            drafted_today = s.scalar(select(func.count()).select_from(Draft).where(Draft.created_at >= ist_day_start()))
            emailed = set(s.scalars(select(func.lower(Draft.to_email))))
            cutoff = utcnow() - timedelta(days=settings.job_freshness_days)
            ids = list(s.scalars(
                select(Job.id)
                .where(Job.status == JobStatus.SCORED, Job.score >= settings.min_score, Job.fetched_at >= cutoff)
                .order_by(Job.score.desc(), Job.posted_at.desc().nulls_last())
            ))
        remaining = settings.daily_draft_target - drafted_today
        if remaining <= 0:
            self.log("draft", "info", f"daily target already met ({drafted_today} drafts today)")
            return
        if not ids:
            self.log("draft", "info", "no good-fit jobs waiting for a draft")
            return

        lookups = 0
        companies_this_run: set[str] = set()
        with GmailDrafts(settings.gmail_address, settings.gmail_app_password) as gmail:
            for job_id in ids:
                if remaining <= 0 or lookups >= settings.max_contact_lookups:
                    break
                if not self.llm.any_available:
                    self.log("draft", "error", "all LLM providers exhausted - drafts will continue next run")
                    break
                with session_scope() as s:
                    job = s.get(Job, job_id)
                    key = company_key(job.company)
                    if key in companies_this_run:
                        continue
                    company = s.scalar(select(Company).where(Company.name_key == key))
                    if company is None:
                        company = Company(name_key=key, name=job.company)
                        s.add(company)
                        s.flush()
                    last = as_aware(company.last_drafted_at)
                    if last and last > utcnow() - timedelta(days=settings.company_cooldown_days):
                        job.status_reason = "company contacted recently (cooldown)"
                        continue
                    if (not job.jd_emails and company.hunter_checked_at is None and company.apollo_checked_at is None
                            and not self.apollo.enabled and self.hunter.waiting_for_tomorrow):
                        # Stays queued instead of landing in "apply manually": tomorrow's Hunter share can look it up.
                        job.status_reason = "waiting for tomorrow's Hunter searches"
                        self.stats["waiting_for_hunter"] += 1
                        continue
                    companies_this_run.add(key)

                    lookups += 1
                    log = lambda level, msg: self.log("contacts", level, msg, job.id)
                    cand = find_contact(job, company, self.apollo, self.hunter, log)
                    if cand is None or not cand.email:
                        job.status, job.status_reason = JobStatus.NO_CONTACT, "no contact found - apply directly"
                        self.stats["no_contact"] += 1
                        continue
                    if cand.email.lower() in emailed:
                        job.status, job.status_reason = JobStatus.NO_CONTACT, f"{cand.email} already emailed before"
                        self.stats["no_contact"] += 1
                        continue

                    contact = Contact(job_id=job.id, name=cand.name, title=cand.title, email=cand.email,
                                      source=cand.source, confidence=cand.confidence, linkedin_url=cand.linkedin_url)
                    s.add(contact)
                    s.flush()

                    try:
                        subject, body, provider = write_email(
                            self.llm, settings, self.resume_text, job, cand.first_name, cand.title)
                    except (AllProvidersFailed, ProviderError) as e:
                        job.status_reason = f"email writing failed: {str(e)[:300]}"
                        self.log("draft", "error", f"writing failed for #{job.id}: {e}", job.id)
                        continue

                    msg = build_message(settings.gmail_address, settings.candidate_name, cand.email, cand.name, subject, body)
                    try:
                        gmail.save_draft(msg)
                    except Exception as e:
                        # IMAP problems don't fix themselves mid-run. Stop, but keep the paid-for contact lookup.
                        job.status_reason = f"gmail draft failed: {str(e)[:300]}"
                        self.log("draft", "error", f"Gmail APPEND failed, stopping drafts: {e}", job.id)
                        break
                    s.add(Draft(job_id=job.id, contact_id=contact.id, run_id=self.run_id, to_email=cand.email,
                                subject=subject, body=body, llm_provider=provider))
                    job.status, job.status_reason = JobStatus.DRAFTED, None
                    company.last_drafted_at = utcnow()
                    emailed.add(cand.email.lower())
                    remaining -= 1
                    self.stats["drafted"] += 1
                    self.stats[f"contact_via_{cand.source}"] += 1

    # ----------------------------------------------------------------------- LinkedIn notes

    def linkedin_stage(self) -> None:
        """A connection note for every good-fit job (emailed or not), one company at a time, best scores first."""
        settings = self.settings
        with session_scope() as s:
            made_today = s.scalar(
                select(func.count()).select_from(LinkedInNote).where(LinkedInNote.created_at >= ist_day_start()))
            recent = utcnow() - timedelta(days=settings.company_cooldown_days)
            noted = {company_key(c) for c in s.scalars(
                select(Job.company).join(LinkedInNote, LinkedInNote.job_id == Job.id).where(LinkedInNote.created_at >= recent))}
            cutoff = utcnow() - timedelta(days=settings.job_freshness_days)
            ids = list(s.scalars(
                select(Job.id)
                .outerjoin(LinkedInNote, LinkedInNote.job_id == Job.id)
                .where(LinkedInNote.id.is_(None), Job.score >= settings.min_score, Job.fetched_at >= cutoff,
                       Job.status.in_([JobStatus.SCORED, JobStatus.NO_CONTACT, JobStatus.DRAFTED]))
                .order_by(Job.score.desc(), Job.posted_at.desc().nulls_last())
            ))
        remaining = settings.linkedin_daily_notes - made_today
        for job_id in ids:
            if remaining <= 0:
                break
            if not self.llm.any_available:
                self.log("linkedin", "error", "all LLM providers exhausted - LinkedIn notes will continue next run")
                break
            with session_scope() as s:
                job = s.get(Job, job_id)
                key = company_key(job.company)
                if key in noted:
                    continue
                try:
                    note, provider = write_note(self.llm, settings, self.resume_text, job)
                except (AllProvidersFailed, ProviderError) as e:
                    self.log("linkedin", "warning", f"note failed for #{job.id}: {e}", job.id)
                    continue
                # Naukri/Indeed jobs don't carry the LinkedIn page; another listing from the same company might.
                page = job.company_linkedin_url or s.scalar(
                    select(Job.company_linkedin_url)
                    .where(func.lower(Job.company) == job.company.lower(), Job.company_linkedin_url.is_not(None))
                    .limit(1))
                s.add(LinkedInNote(job_id=job.id, run_id=self.run_id, note=note, company_linkedin_url=page,
                                   llm_provider=provider))
                noted.add(key)
                remaining -= 1
                self.stats["linkedin_notes"] += 1


def _keep_awake(url: str, done: threading.Event) -> None:
    # Render's free plan sleeps after 15 min without inbound requests, even mid-run. A request to our own public
    # URL counts, so a run started from the dashboard survives the tab being closed.
    while not done.wait(300):
        try:
            httpx.get(f"{url}/health", timeout=30)
        except httpx.HTTPError:
            pass


def run_pipeline(run_id: int) -> None:
    done = threading.Event()
    if url := os.environ.get("RENDER_EXTERNAL_URL"):  # set by Render
        threading.Thread(target=_keep_awake, args=(url, done), daemon=True).start()
    try:
        PipelineRun(run_id).execute()
    finally:
        done.set()
