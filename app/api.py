import hmac
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.gmail import GmailDrafts
from app.models import Draft, DraftStatus, Event, Job, JobStatus, ProviderUsage, Run, utcnow
from app.pipeline.runner import ist_day_start, run_pipeline, start_run
from app.providers.finders import Hunter


def require_key(x_api_key: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_api_key, get_settings().api_key):
        raise HTTPException(status_code=401, detail="invalid API key")


router = APIRouter(dependencies=[Depends(require_key)])


# --------------------------------------------------------------------------- serializers


def job_out(j: Job, full: bool = False) -> dict:
    d = j.score_details or {}
    out = {
        "id": j.id, "title": j.title, "company": j.company, "location": j.location, "is_remote": j.is_remote,
        "source": j.source, "url": j.url, "apply_url": j.apply_url, "status": j.status, "status_reason": j.status_reason,
        "score": j.score, "summary": d.get("summary"), "fit_points": d.get("fit_points", []), "gaps": d.get("gaps", []),
        "experience": j.experience_text, "salary": j.salary_text, "posted_at": j.posted_at, "fetched_at": j.fetched_at,
    }
    if full:
        out["description"] = j.description
    return out


def draft_out(d: Draft) -> dict:
    return {
        "id": d.id, "status": d.status, "subject": d.subject, "body": d.body, "to_email": d.to_email,
        "contact_name": d.contact.name if d.contact else None,
        "contact_title": d.contact.title if d.contact else None,
        "contact_source": d.contact.source if d.contact else None,
        "contact_confidence": d.contact.confidence if d.contact else None,
        "created_at": d.created_at, "status_changed_at": d.status_changed_at, "llm_provider": d.llm_provider,
        "job": {"id": d.job.id, "title": d.job.title, "company": d.job.company, "url": d.job.url, "score": d.job.score},
    }


def run_out(r: Run) -> dict:
    return {"id": r.id, "trigger": r.trigger, "status": r.status, "started_at": r.started_at,
            "finished_at": r.finished_at, "stats": r.stats or {}, "error": r.error}


# --------------------------------------------------------------------------- runs


@router.post("/runs", status_code=202)
def trigger_run(background: BackgroundTasks, trigger: Literal["cron", "manual"] = "manual"):
    run_id = start_run(trigger)
    if run_id is None:
        raise HTTPException(status_code=409, detail="a run is already in progress")
    background.add_task(run_pipeline, run_id)
    return {"run_id": run_id}


@router.get("/runs")
def list_runs(limit: int = Query(20, le=100), db: Session = Depends(get_db)):
    return [run_out(r) for r in db.scalars(select(Run).order_by(Run.id.desc()).limit(limit))]


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404)
    events = db.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id))
    return {**run_out(run), "events": [
        {"id": e.id, "stage": e.stage, "level": e.level, "message": e.message, "job_id": e.job_id, "created_at": e.created_at}
        for e in events
    ]}


# --------------------------------------------------------------------------- overview


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    today = ist_day_start()
    week = utcnow() - timedelta(days=7)
    draft_counts = dict(db.execute(select(Draft.status, func.count()).group_by(Draft.status)).all())
    job_counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    total_drafts = sum(draft_counts.values())
    sent = draft_counts.get(DraftStatus.SENT, 0) + draft_counts.get(DraftStatus.REPLIED, 0)
    last_run = db.scalar(select(Run).order_by(Run.id.desc()).limit(1))
    return {
        "drafts_today": db.scalar(select(func.count()).select_from(Draft).where(Draft.created_at >= today)),
        "drafts_this_week": db.scalar(select(func.count()).select_from(Draft).where(Draft.created_at >= week)),
        "daily_target": get_settings().daily_draft_target,
        "drafts_by_status": draft_counts,
        "jobs_by_status": job_counts,
        "reply_rate": round(draft_counts.get(DraftStatus.REPLIED, 0) / sent * 100, 1) if sent else None,
        "total_drafts": total_drafts,
        "errors_last_7d": db.scalar(select(func.count()).select_from(Event).where(Event.level == "error", Event.created_at >= week)),
        "last_run": run_out(last_run) if last_run else None,
    }


@router.get("/providers")
def providers(db: Session = Depends(get_db)):
    s = get_settings()
    since = date.today() - timedelta(days=30)
    rows = db.scalars(select(ProviderUsage).where(ProviderUsage.day >= since).order_by(ProviderUsage.day.desc()))
    return {
        "configured": {
            "gemini": bool(s.gemini_api_key), "groq": bool(s.groq_api_key),
            "apollo": bool(s.apollo_api_key), "hunter": bool(s.hunter_api_key),
        },
        "hunter_account": Hunter(s.hunter_api_key).remaining_searches(),
        "usage": [
            {"provider": r.provider, "day": r.day, "calls": r.calls, "failures": r.failures,
             "exhausted": r.exhausted, "last_error": r.last_error}
            for r in rows
        ],
    }


@router.get("/events")
def events(level: str | None = None, limit: int = Query(100, le=500), db: Session = Depends(get_db)):
    q = select(Event).order_by(Event.id.desc()).limit(limit)
    if level:
        q = q.where(Event.level == level)
    return [{"id": e.id, "run_id": e.run_id, "job_id": e.job_id, "stage": e.stage, "level": e.level,
             "message": e.message, "created_at": e.created_at} for e in db.scalars(q)]


# --------------------------------------------------------------------------- drafts


@router.get("/drafts")
def list_drafts(status: str | None = None, limit: int = Query(100, le=500), db: Session = Depends(get_db)):
    q = select(Draft).options(selectinload(Draft.job), selectinload(Draft.contact)).order_by(Draft.id.desc()).limit(limit)
    if status:
        q = q.where(Draft.status == status)
    return [draft_out(d) for d in db.scalars(q)]


class DraftUpdate(BaseModel):
    status: Literal["drafted", "sent", "replied", "skipped"]


@router.patch("/drafts/{draft_id}")
def update_draft(draft_id: int, body: DraftUpdate, db: Session = Depends(get_db)):
    d = db.get(Draft, draft_id)
    if not d:
        raise HTTPException(404)
    d.status, d.status_changed_at = body.status, utcnow()
    db.flush()
    return draft_out(d)


# --------------------------------------------------------------------------- jobs


@router.get("/jobs")
def list_jobs(
    status: str | None = None,
    min_score: int | None = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
):
    q = select(Job).order_by(Job.id.desc()).limit(limit)
    if status:
        q = q.where(Job.status == status)
    if min_score is not None:
        q = q.where(Job.score >= min_score).order_by(None).order_by(Job.score.desc(), Job.id.desc())
    return [job_out(j) for j in db.scalars(q)]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if not j:
        raise HTTPException(404)
    return job_out(j, full=True)


class JobUpdate(BaseModel):
    status: Literal["scored", "no_contact", "low_score", "filtered_out"]


@router.patch("/jobs/{job_id}")
def update_job(job_id: int, body: JobUpdate, db: Session = Depends(get_db)):
    """Lets you push a job back into the draft queue, or dismiss it."""
    j = db.get(Job, job_id)
    if not j:
        raise HTTPException(404)
    j.status, j.status_reason = body.status, "changed manually"
    db.flush()
    return job_out(j)


# --------------------------------------------------------------------------- checks


@router.get("/check/gmail")
def check_gmail():
    s = get_settings()
    try:
        with GmailDrafts(s.gmail_address, s.gmail_app_password) as g:
            return {"ok": True, "drafts_folder": g.check()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
