from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """Some drivers hand back naive datetimes; everything we store is UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


class JobStatus:
    NEW = "new"
    FILTERED_OUT = "filtered_out"  # failed cheap rule filter
    SCORED = "scored"  # passed LLM scoring, waiting for contact lookup
    LOW_SCORE = "low_score"
    NO_CONTACT = "no_contact"  # good fit but nobody to email -> apply manually
    DRAFTED = "drafted"
    ERROR = "error"


class DraftStatus:
    DRAFTED = "drafted"
    SENT = "sent"
    REPLIED = "replied"
    SKIPPED = "skipped"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(300), index=True)
    company_domain: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(300))
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    experience_text: Mapped[str | None] = mapped_column(String(100))
    salary_text: Mapped[str | None] = mapped_column(String(100))
    jd_emails: Mapped[list | None] = mapped_column(JSON)
    posted_at: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    status: Mapped[str] = mapped_column(String(30), default=JobStatus.NEW, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_details: Mapped[dict | None] = mapped_column(JSON)  # summary, fit_points, gaps, provider
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))

    contacts: Mapped[list["Contact"]] = relationship(back_populates="job")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="job")


class Company(Base):
    """Per-company cache so we never pay twice for the same lookup."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_key: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(200))
    domain_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_pattern: Mapped[str | None] = mapped_column(String(100))
    hunter_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apollo_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str] = mapped_column(String(320), index=True)
    source: Mapped[str] = mapped_column(String(30))  # jd | apollo | hunter | pattern
    confidence: Mapped[int | None] = mapped_column(Integer)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="contacts")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    to_email: Mapped[str] = mapped_column(String(320), index=True)
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    llm_provider: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default=DraftStatus.DRAFTED, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="drafts")
    contact: Mapped[Contact] = relationship()


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))  # cron | manual
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running | success | partial | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    """Run log shown on the dashboard (mostly warnings/errors)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[str] = mapped_column(String(30))
    level: Mapped[str] = mapped_column(String(10), index=True)  # info | warning | error
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    __table_args__ = (UniqueConstraint("provider", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    day: Mapped[date] = mapped_column(Date)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class ResumeCache(Base):
    __tablename__ = "resume_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_url: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
