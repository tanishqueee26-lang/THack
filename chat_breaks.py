"""Creator-approved chat-break activities for short streamer absences."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
from datetime import timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from backend import config, twitch as tw
from backend.db import Base, SessionLocal, User, UserSession, get_db, utcnow

router = APIRouter()
log = logging.getLogger("questcast.chat_breaks")


class ChatBreak(Base):
    __tablename__ = "workspace_chat_breaks"
    __table_args__ = (
        Index("uq_workspace_active_chat_break", "workspace_key", unique=True,
              sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    started_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    ends_at: Mapped[object] = mapped_column(DateTime)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    next_activity_at: Mapped[object] = mapped_column(DateTime)


class ChatBreakActivity(Base):
    __tablename__ = "workspace_chat_break_activities"
    __table_args__ = (UniqueConstraint("break_id", "position", name="uq_chat_break_activity_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    break_id: Mapped[str] = mapped_column(ForeignKey("workspace_chat_breaks.id", ondelete="CASCADE"), index=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))
    title: Mapped[str] = mapped_column(String(90))
    prompt: Mapped[str] = mapped_column(String(240))
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    meme_query: Mapped[str] = mapped_column(String(50), default="")
    meme_caption: Mapped[str] = mapped_column(String(180), default="")
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class ChatBreakVote(Base):
    __tablename__ = "workspace_chat_break_votes"
    __table_args__ = (UniqueConstraint("activity_id", "voter_hash", name="uq_chat_break_activity_voter"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[str] = mapped_column(ForeignKey("workspace_chat_break_activities.id", ondelete="CASCADE"), index=True)
    voter_hash: Mapped[str] = mapped_column(String(64))
    choice_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class ChatActivityDraft(BaseModel):
    kind: Literal["poll", "meme"]
    title: str = Field(min_length=3, max_length=90)
    prompt: str = Field(min_length=3, max_length=240)
    options: list[str] = Field(default_factory=list, max_length=3)
    meme_query: str = Field(default="", max_length=50)
    meme_caption: str = Field(default="", max_length=180)

    @field_validator("options")
    @classmethod
    def clean_options(cls, value: list[str]) -> list[str]:
        return [" ".join(item.split())[:60] for item in value]

    @model_validator(mode="after")
    def validate_activity(self):
        if self.kind == "poll" and len(self.options) != 3:
            raise ValueError("Polls need exactly three choices")
        if self.kind == "meme" and (not self.meme_query or not self.meme_caption):
            raise ValueError("Meme activities need a search phrase and caption")
        return self


class PrepareBreakIn(BaseModel):
    duration_minutes: int = Field(default=12, ge=3, le=30)
    interval_seconds: int = Field(default=180, ge=120, le=600)
    theme: str = Field(default="gaming break", min_length=2, max_length=80)


class StartBreakIn(PrepareBreakIn):
    activities: list[ChatActivityDraft] = Field(min_length=1, max_length=10)


class StopBreakIn(BaseModel):
    pass


def _workspace_owner(request: Request, db) -> tuple[str, User | None]:
    if config.operating_mode() == "simulation":
        return "demo", None
    sid = request.cookies.get("qc_session")
    session = db.get(UserSession, sid) if sid else None
    if not session or utcnow() - session.created_at > timedelta(days=config.SESSION_DAYS):
        raise HTTPException(401, "Connect an authenticated Twitch creator account first")
    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(401, "Creator session is no longer valid")
    return f"twitch:{user.id}", user


def _fallback_activities(count: int, theme: str) -> list[ChatActivityDraft]:
    templates = [
        ChatActivityDraft(kind="poll", title="Chat picks the next move", prompt=f"What's the best next move for this {theme}?", options=["Play it safe", "Go full chaos", "Ask chat again"]),
        ChatActivityDraft(kind="meme", title="The break reaction", prompt="Meme moment on the stream overlay", meme_query="gaming reaction funny", meme_caption="When chat realizes the streamer stepped away for one minute"),
        ChatActivityDraft(kind="poll", title="Snack side quest", prompt="Choose the official break snack", options=["Crunchy snack", "Something sweet", "Water and focus"]),
        ChatActivityDraft(kind="meme", title="Chat energy check", prompt="Fresh G-rated reaction GIF on the stream overlay", meme_query="funny excited reaction", meme_caption="Chat holding the fort like absolute legends"),
        ChatActivityDraft(kind="poll", title="Return soundtrack", prompt="What should play when the streamer returns?", options=["Victory fanfare", "Dramatic entrance", "Nothing, act natural"]),
        ChatActivityDraft(kind="meme", title="Lobby status", prompt="G-rated gaming GIF for the break", meme_query="gaming waiting funny", meme_caption="The squad keeping the lobby warm"),
        ChatActivityDraft(kind="poll", title="Tiny prediction", prompt="What happens in the first minute back?", options=["Instant win", "Unexpected chaos", "Another quick break"]),
        ChatActivityDraft(kind="meme", title="Unexpected plot twist", prompt="G-rated plot-twist reaction GIF", meme_query="funny plot twist reaction", meme_caption="Chat after the streamer says 'one last match'"),
        ChatActivityDraft(kind="poll", title="Chat's loadout", prompt="Pick chat's imaginary loadout", options=["Maximum snacks", "Maximum luck", "Maximum emotes"]),
        ChatActivityDraft(kind="meme", title="We got this", prompt="G-rated team reaction GIF", meme_query="funny team celebration", meme_caption="Chat carrying the vibes until the streamer gets back"),
    ]
    return [templates[index % len(templates)] for index in range(count)]


async def prepare_activities(body: PrepareBreakIn) -> dict:
    count = min(10, math.ceil(body.duration_minutes * 60 / body.interval_seconds))
    if not config.OPENAI_API_KEY:
        return {"activities": [activity.model_dump() for activity in _fallback_activities(count, body.theme)],
                "generated_by": "curated_fallback", "warning": "Add OPENAI_API_KEY on the backend to draft custom activities."}

    activity_schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["poll", "meme"]},
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "meme_query": {"type": "string"},
            "meme_caption": {"type": "string"},
        },
        "required": ["kind", "title", "prompt", "options", "meme_query", "meme_caption"],
        "additionalProperties": False,
    }
    schema = {"type": "object", "properties": {"activities": {"type": "array", "items": activity_schema}},
              "required": ["activities"], "additionalProperties": False}
    instructions = (
        "Create short, upbeat, creator-safe Twitch chat break activities for a gaming stream. "
        "Make a varied sequence of exactly the requested count, alternating polls and meme moments. "
        "Polls need three short choices. Meme moments need one short caption and a G-rated GIPHY search phrase. "
        "Avoid adult, hateful, political, violent, dangerous, or insulting content; do not use copyrighted lyrics. "
        "These are drafts for creator review. Do not claim anything about the stream or viewers."
    )
    prompt = f"Create exactly {count} activities for a {body.duration_minutes}-minute {body.theme} break."
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                json={"model": config.OPENAI_MODEL, "instructions": instructions, "input": prompt,
                      "max_output_tokens": 2200,
                      "text": {"format": {"type": "json_schema", "name": "chat_break_activities",
                                             "strict": True, "schema": schema}}},
            )
            response.raise_for_status()
        data = response.json()
        output = "".join(
            str(part.get("text", ""))
            for item in data.get("output", []) if item.get("type") == "message"
            for part in item.get("content", []) if part.get("type") == "output_text"
        )
        parsed = json.loads(output)
        activities = [ChatActivityDraft.model_validate(item) for item in parsed["activities"]]
        if len(activities) != count:
            raise ValueError("The model returned an unexpected number of activities")
        for index, activity in enumerate(activities):
            if index % 2 == 0 and activity.kind != "poll":
                raise ValueError("The generated activity sequence did not alternate as requested")
            if index % 2 == 1 and activity.kind != "meme":
                raise ValueError("The generated activity sequence did not alternate as requested")
        return {"activities": [activity.model_dump() for activity in activities], "generated_by": "openai_responses"}
    except Exception as exc:
        log.info("Chat break draft generation fell back (%s)", type(exc).__name__)
        return {"activities": [activity.model_dump() for activity in _fallback_activities(count, body.theme)],
                "generated_by": "curated_fallback", "warning": "OpenAI was unavailable; review the curated drafts below."}


def _counts(db, activity: ChatBreakActivity) -> list[int]:
    if activity.kind != "poll":
        return []
    choices = len(json.loads(activity.options_json or "[]"))
    rows = db.query(ChatBreakVote.choice_index, func.count(ChatBreakVote.id)).filter_by(activity_id=activity.id).group_by(ChatBreakVote.choice_index).all()
    counts = [0] * choices
    for index, count in rows:
        if 0 <= index < choices:
            counts[index] = count
    return counts


def _activity_dict(db, activity: ChatBreakActivity) -> dict:
    return {"id": activity.id, "kind": activity.kind, "title": activity.title, "prompt": activity.prompt,
            "options": json.loads(activity.options_json or "[]"), "meme_query": activity.meme_query,
            "meme_caption": activity.meme_caption, "status": activity.status, "votes": _counts(db, activity)}


def _session_state(db, session: ChatBreak | None) -> dict | None:
    if not session:
        return None
    current = db.query(ChatBreakActivity).filter_by(break_id=session.id, status="active").first()
    activities = db.query(ChatBreakActivity).filter_by(break_id=session.id).order_by(ChatBreakActivity.position).all()
    return {"id": session.id, "status": session.status, "started_at": session.started_at.isoformat() + "Z",
            "ends_at": session.ends_at.isoformat() + "Z", "interval_seconds": session.interval_seconds,
            "current": _activity_dict(db, current) if current else None,
            "activities": [_activity_dict(db, activity) for activity in activities],
            "mode": "simulation" if session.workspace_key == "demo" else "live"}


def _active_session(db, workspace_key: str) -> ChatBreak | None:
    session = db.query(ChatBreak).filter_by(workspace_key=workspace_key, status="active").first()
    if session and session.ends_at <= utcnow():
        session.status = "completed"
        for activity in db.query(ChatBreakActivity).filter_by(break_id=session.id, status="active"):
            activity.status = "closed"
        db.commit()
        return None
    return session


def _chat_text(activity: ChatBreakActivity) -> str:
    if activity.kind == "poll":
        return f"{activity.title}: {activity.prompt} Vote in chat with !vote A, !vote B, or !vote C."
    return f"{activity.title}: {activity.meme_caption} Check the stream overlay for the meme!"


async def _send_to_twitch(db, session: ChatBreak, activity: ChatBreakActivity | None = None, message: str | None = None) -> None:
    if config.operating_mode() != "live" or not session.workspace_key.startswith("twitch:"):
        return
    try:
        user_id = int(session.workspace_key.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    user = db.get(User, user_id)
    if not user or "user:write:chat" not in user.scopes.split():
        return
    text_to_send = message or (_chat_text(activity) if activity else "")
    if not text_to_send:
        return
    try:
        await tw.send_chat(user, db, text_to_send[:450])
    except Exception as exc:
        log.warning("Could not send chat-break message (%s)", type(exc).__name__)


@router.post("/chat-break/prepare")
async def prepare_break(body: PrepareBreakIn, request: Request, db=Depends(get_db)):
    _workspace_owner(request, db)
    return await prepare_activities(body)


@router.post("/chat-break/start")
async def start_break(body: StartBreakIn, request: Request, db=Depends(get_db)):
    key, user = _workspace_owner(request, db)
    if user and "user:read:chat" not in user.scopes.split():
        raise HTTPException(403, "Reauthorize Twitch with the user:read:chat scope to receive !vote commands.")
    if user and "user:write:chat" not in user.scopes.split():
        raise HTTPException(403, "Reauthorize Twitch with the user:write:chat scope to post break activities.")
    existing = _active_session(db, key)
    if existing:
        raise HTTPException(409, "A chat break is already active")
    expected = min(10, math.ceil(body.duration_minutes * 60 / body.interval_seconds))
    if len(body.activities) != expected:
        raise HTTPException(422, f"Review exactly {expected} activities before starting this break")
    now = utcnow()
    session = ChatBreak(id=secrets.token_hex(16), workspace_key=key, started_at=now,
                        ends_at=now + timedelta(minutes=body.duration_minutes),
                        interval_seconds=body.interval_seconds,
                        next_activity_at=now + timedelta(seconds=body.interval_seconds))
    db.add(session)
    db.flush()
    first = None
    for index, draft in enumerate(body.activities):
        activity = ChatBreakActivity(id=secrets.token_hex(16), break_id=session.id, workspace_key=key,
            position=index, kind=draft.kind, title=draft.title, prompt=draft.prompt,
            options_json=json.dumps(draft.options), meme_query=draft.meme_query, meme_caption=draft.meme_caption,
            status="active" if index == 0 else "queued")
        db.add(activity)
        if index == 0:
            first = activity
    db.commit()
    if first:
        await _send_to_twitch(db, session, first)
    return _session_state(db, session)


@router.get("/chat-break")
def get_break(request: Request, db=Depends(get_db)):
    key, _ = _workspace_owner(request, db)
    return _session_state(db, _active_session(db, key))


@router.post("/chat-break/stop")
async def stop_break(request: Request, db=Depends(get_db)):
    key, _ = _workspace_owner(request, db)
    session = _active_session(db, key)
    if not session:
        raise HTTPException(404, "No chat break is active")
    current = db.query(ChatBreakActivity).filter_by(break_id=session.id, status="active").first()
    session.status = "stopped"
    if current:
        current.status = "closed"
    db.query(ChatBreakActivity).filter_by(break_id=session.id, status="queued").update({"status": "closed"})
    db.commit()
    await _send_to_twitch(db, session, message="The chat break is over — streamer is back soon. Thanks for keeping the vibes going!")
    return _session_state(db, session)


def register_vote(db, workspace_key: str, payload: dict) -> str | None:
    """Count only a verified EventSub chat vote; raw text and Twitch IDs are not stored."""
    session = _active_session(db, workspace_key)
    if not session:
        return None
    activity = db.query(ChatBreakActivity).filter_by(break_id=session.id, status="active", kind="poll").first()
    if not activity:
        return None
    message = str((payload.get("message") or {}).get("text") or "").strip().split()
    if not message or message[0].casefold() != "!vote":
        return None
    viewer_id = str(payload.get("chatter_user_id") or "")
    if not viewer_id:
        return None
    voter_hash = hashlib.sha256(f"{activity.id}:{viewer_id}".encode("utf-8")).hexdigest()
    from backend.workspace import ChatCommandCooldown
    now = utcnow()
    cooldown = db.query(ChatCommandCooldown).filter_by(
        workspace_key=workspace_key, participant_id=voter_hash, command="!vote").first()
    if cooldown and now - cooldown.last_used_at < timedelta(seconds=5):
        return None
    if cooldown:
        cooldown.last_used_at = now
    else:
        db.add(ChatCommandCooldown(workspace_key=workspace_key, participant_id=voter_hash,
                                   command="!vote", last_used_at=now))
    if len(message) != 2:
        db.commit()
        return "Vote with !vote A, !vote B, or !vote C."
    options = json.loads(activity.options_json or "[]")
    choice = message[1].upper()
    index = ord(choice) - ord("A") if len(choice) == 1 and "A" <= choice <= "C" else -1
    if not (0 <= index < len(options)):
        db.commit()
        return "That choice is not in this poll. Vote with !vote A, !vote B, or !vote C."
    vote = db.query(ChatBreakVote).filter_by(activity_id=activity.id, voter_hash=voter_hash).first()
    if vote:
        vote.choice_index = index
        vote.created_at = utcnow()
    else:
        db.add(ChatBreakVote(activity_id=activity.id, voter_hash=voter_hash, choice_index=index))
    db.commit()
    return "Vote updated — check the stream overlay for the live results."


async def run_break_scheduler() -> None:
    """Advance through the creator-approved queue and close expired breaks."""
    while True:
        await asyncio.sleep(5)
        try:
            with SessionLocal() as db:
                now = utcnow()
                due = db.query(ChatBreak).filter(ChatBreak.status == "active",
                    (ChatBreak.ends_at <= now) | (ChatBreak.next_activity_at <= now)).all()
                for session in due:
                    if session.ends_at <= now:
                        active = db.query(ChatBreakActivity).filter_by(break_id=session.id, status="active").first()
                        if active:
                            active.status = "closed"
                        db.query(ChatBreakActivity).filter_by(break_id=session.id, status="queued").update({"status": "closed"})
                        session.status = "completed"
                        db.commit()
                        await _send_to_twitch(db, session, message="The break is over — streamer is back soon. Thanks for hanging out!")
                        continue
                    current = db.query(ChatBreakActivity).filter_by(break_id=session.id, status="active").first()
                    next_activity = db.query(ChatBreakActivity).filter_by(break_id=session.id, status="queued").order_by(ChatBreakActivity.position).first()
                    if current:
                        current.status = "closed"
                    if not next_activity:
                        session.status = "completed"
                        db.commit()
                        await _send_to_twitch(db, session, message="That's all for this break. Thanks for keeping chat cozy!")
                        continue
                    next_activity.status = "active"
                    session.next_activity_at = now + timedelta(seconds=session.interval_seconds)
                    db.commit()
                    await _send_to_twitch(db, session, next_activity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Chat-break scheduler recovered from an error (%s)", type(exc).__name__)
