import logging
from datetime import date

from sqlalchemy import select

from app.db import session_scope
from app.models import Event, ProviderUsage

logger = logging.getLogger("outreach")


def log_event(run_id: int | None, stage: str, level: str, message: str, job_id: int | None = None) -> None:
    getattr(logger, "warning" if level == "warning" else level, logger.info)(f"[{stage}] {message}")
    try:
        with session_scope() as s:
            s.add(Event(run_id=run_id, job_id=job_id, stage=stage, level=level, message=message[:4000]))
    except Exception:  # logging must never break the pipeline
        logger.exception("failed to persist event")


def calls_today(provider: str) -> int:
    with session_scope() as s:
        row = s.scalar(select(ProviderUsage).where(ProviderUsage.provider == provider, ProviderUsage.day == date.today()))
        return row.calls if row else 0


def record_usage(provider: str, *, ok: bool, exhausted: bool = False, error: str | None = None) -> None:
    try:
        with session_scope() as s:
            row = s.scalar(select(ProviderUsage).where(ProviderUsage.provider == provider, ProviderUsage.day == date.today()))
            if row is None:
                row = ProviderUsage(provider=provider, day=date.today(), calls=0, failures=0, exhausted=False)
                s.add(row)
            row.calls += 1
            if not ok:
                row.failures += 1
                row.last_error = (error or "")[:1000]
            if exhausted:
                row.exhausted = True
    except Exception:
        logger.exception("failed to record provider usage")
