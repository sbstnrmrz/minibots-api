from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import DATABASE_URL


def _sqlalchemy_url(url: str) -> str:
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme in ("postgresql", "postgres"):
            return f"postgresql+psycopg://{rest}"
    return url


engine = create_engine(_sqlalchemy_url(DATABASE_URL))
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_context():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
