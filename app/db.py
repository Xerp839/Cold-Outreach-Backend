from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _normalize_url(url: str) -> str:
    # Supabase hands out postgres:// / postgresql:// URLs; SQLAlchemy needs the psycopg3 driver named.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


_url = _normalize_url(get_settings().database_url)
engine = create_engine(
    _url,
    pool_pre_ping=True,
    # Supabase's transaction pooler doesn't support prepared statements.
    connect_args={"prepare_threshold": None} if _url.startswith("postgresql+psycopg") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db():
    with session_scope() as session:
        yield session
