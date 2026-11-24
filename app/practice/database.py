"""Persistent accounts and practice history. SQLite locally, PostgreSQL in hosting."""
import os
from pathlib import Path

from sqlalchemy import Column, Float, ForeignKey, Integer, JSON, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(String(64), primary_key=True)
    username = Column(String(32), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)


class Login(Base):
    __tablename__ = "logins"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    csrf = Column(String(64), nullable=False)
    expires_at = Column(Float, nullable=False)


class RateLimit(Base):
    __tablename__ = "auth_limits"
    key = Column(String(64), primary_key=True)
    count = Column(Integer, nullable=False)
    reset_at = Column(Float, nullable=False)


class PracticeSession(Base):
    __tablename__ = "practice_sessions"
    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    started_at = Column(Float, nullable=False)
    deadline = Column(Float, nullable=False)
    finished_at = Column(Float)
    settings = Column(JSON, nullable=False)
    problem_ids = Column(JSON, nullable=False)


class Work(Base):
    __tablename__ = "practice_work"
    session_id = Column(String(64), ForeignKey("practice_sessions.id"), primary_key=True)
    problem_id = Column(String(100), primary_key=True)
    code = Column(String(50000), nullable=False, default="")
    result = Column(JSON)
    revision = Column(Integer, nullable=False, default=0)


class Database:
    def __init__(self, url=None):
        if url is None:
            url = os.environ.get("DATABASE_URL")
        if not url:
            directory = Path(os.environ.get("PAIR_STATE_DIR", "var"))
            directory.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{directory / 'pair.db'}"
        if url.startswith(("postgres://", "postgresql://")):
            url = "postgresql+psycopg://" + url.split("://", 1)[1]
        self.engine = create_engine(
            url, pool_pre_ping=True,
            **({"connect_args": {"check_same_thread": False, "timeout": 15}} if url.startswith("sqlite") else {}),
        )
        # Serialize first-start schema creation across local workers.
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            elif self.engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(72417301)")
            Base.metadata.create_all(connection)
            connection.commit()
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def close(self):
        self.engine.dispose()
