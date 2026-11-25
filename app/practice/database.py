"""Persistent accounts and practice history. SQLite locally, PostgreSQL in hosting."""
import os
from pathlib import Path

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, JSON, String, create_engine, false
from sqlalchemy.orm import declarative_base, sessionmaker

from app.practice.migrations import upgrade

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


class Rating(Base):
    __tablename__ = "ranked_ratings"
    user_id = Column(String(64), ForeignKey("users.id"), primary_key=True)
    rating = Column(Integer, nullable=False, default=1000)
    matches_played = Column(Integer, nullable=False, default=0)
    updated_at = Column(Float, nullable=False)


class RankedMatch(Base):
    __tablename__ = "ranked_matches"
    id = Column(String(64), primary_key=True)
    problem_ids = Column(JSON, nullable=False)
    started_at = Column(Float, nullable=False)
    deadline = Column(Float, nullable=False)
    finished_at = Column(Float)
    status = Column(String(16), nullable=False, default="active")


class RankedParticipant(Base):
    __tablename__ = "ranked_participants"
    match_id = Column(String(64), ForeignKey("ranked_matches.id"), primary_key=True)
    user_id = Column(String(64), ForeignKey("users.id"), primary_key=True)
    rating_before = Column(Integer, nullable=False)
    rating_after = Column(Integer)
    outcome = Column(String(16))


class MatchIntro(Base):
    __tablename__ = "ranked_match_intros"
    match_id = Column(String(64), ForeignKey("ranked_matches.id"), primary_key=True)
    first_user_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    second_user_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    first_ready_at = Column(Float)
    second_ready_at = Column(Float)
    created_at = Column(Float, nullable=False)
    scheduled_at = Column(Float)
    cancelled_at = Column(Float)


class RankedWork(Base):
    __tablename__ = "ranked_work"
    # Keyed per-user too, unlike solo Work: both sides solve the same
    # problem_id in the same match_id, so a (match_id, problem_id) row
    # alone would let one side's submission overwrite the other's.
    match_id = Column(String(64), ForeignKey("ranked_matches.id"), primary_key=True)
    problem_id = Column(String(100), primary_key=True)
    user_id = Column(String(64), ForeignKey("users.id"), primary_key=True)
    code = Column(String(50000), nullable=False, default="")
    result = Column(JSON)
    revision = Column(Integer, nullable=False, default=0)


class Lobby(Base):
    __tablename__ = "practice_lobbies"
    code = Column(String(6), primary_key=True)
    host_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    guest_id = Column(String(64), ForeignKey("users.id"))
    settings = Column(JSON, nullable=False)
    is_private = Column(Boolean, nullable=False, default=False, server_default=false())
    status = Column(String(16), nullable=False, default="waiting")
    created_at = Column(Float, nullable=False)
    host_ready = Column(Integer, nullable=False, default=1)
    guest_ready = Column(Integer, nullable=False, default=0)
    match_id = Column(String(64), ForeignKey("ranked_matches.id"), unique=True)


class LobbySeat(Base):
    __tablename__ = "practice_lobby_seats"
    user_id = Column(String(64), ForeignKey("users.id"), primary_key=True)
    code = Column(String(6), ForeignKey("practice_lobbies.code"), nullable=False, index=True)
    last_seen = Column(Float, nullable=False)


class MatchFinish(Base):
    __tablename__ = "ranked_match_finishes"
    match_id = Column(String(64), ForeignKey("ranked_matches.id"), primary_key=True)
    user_id = Column(String(64), ForeignKey("users.id"), primary_key=True)
    state = Column(String(16), nullable=False)
    finished_at = Column(Float, nullable=False)


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
            upgrade(connection)
            connection.commit()
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def close(self):
        self.engine.dispose()
