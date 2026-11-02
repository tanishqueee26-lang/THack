import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from backend import config
from backend.crypto import decrypt, encrypt

if config.DATABASE_URL.startswith("sqlite:///./"):
    os.makedirs(os.path.dirname(config.DATABASE_URL.replace("sqlite:///", "")) or ".", exist_ok=True)

_sqlite = config.DATABASE_URL.startswith("sqlite")
engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False} if _sqlite else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _fk_on(conn, _):
    if _sqlite:
        conn.execute("PRAGMA foreign_keys=ON")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    twitch_id: Mapped[str] = mapped_column(String(32), unique=True)
    login: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(64))
    avatar: Mapped[str] = mapped_column(String(512), default="")
    access_token_enc: Mapped[str] = mapped_column(Text, default="")   # Fernet-encrypted
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[str] = mapped_column(Text, default="")  # space-separated, from /oauth2/validate

    @property
    def access_token(self) -> str:
        return decrypt(self.access_token_enc) if self.access_token_enc else ""

    @access_token.setter
    def access_token(self, v: str) -> None:
        self.access_token_enc = encrypt(v) if v else ""

    @property
    def refresh_token(self) -> str:
        return decrypt(self.refresh_token_enc) if self.refresh_token_enc else ""

    @refresh_token.setter
    def refresh_token(self, v: str) -> None:
        self.refresh_token_enc = encrypt(v) if v else ""


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True)  # dedupes Twitch retries
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="twitch")  # real Twitch deliveries only
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HealthEvent(Base):
    """Persisted integration health history: status transitions and errors."""
    __tablename__ = "health_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(32))     # eventsub | chat | token | error
    status: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
