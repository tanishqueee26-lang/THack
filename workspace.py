"""Persistent QuestCast workspace: quests, participation, explainable candidates and community memory.

Simulation data is stored under a separate `demo` workspace and never merged with Twitch events.
Live workspace keys are derived from the authenticated Twitch broadcaster identity.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend import config, twitch as tw
from backend.db import Base, User, UserSession, get_db, utcnow, SessionLocal
from backend.window import SlidingWindow

router = APIRouter()
_HYPE_WINDOWS: dict[str, SlidingWindow] = {}
_HYPE_LOCKED: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Quest(Base):
    __tablename__ = "workspace_quests"
    __table_args__ = (Index("uq_workspace_active_quest", "workspace_key", unique=True, sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    objective: Mapped[str] = mapped_column(String(240), default="Community participation")
    target: Mapped[int] = mapped_column(Integer, default=25)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    stages_json: Mapped[str] = mapped_column(Text, default="[]")
    rules_json: Mapped[str] = mapped_column(Text, default='{"one_per_viewer":true}')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Participation(Base):
    __tablename__ = "workspace_participation"
    __table_args__ = (UniqueConstraint("quest_id", "participant_id", name="uq_quest_participant"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("workspace_quests.id", ondelete="CASCADE"), index=True)
    participant_id: Mapped[str] = mapped_column(String(96))
    display_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkspaceEvent(Base):
    __tablename__ = "workspace_events"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(24))
    summary: Mapped[str] = mapped_column(String(240))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)


class Candidate(Base):
    __tablename__ = "workspace_candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(32))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    reasons_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    clip_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    clip_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    clip_status: Mapped[str] = mapped_column(String(24), default="not_requested")
    clip_detail: Mapped[str] = mapped_column(String(500), default="")
    clip_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    title_suggestion: Mapped[str] = mapped_column(String(180), default="")
    caption_suggestion: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MemoryEntry(Base):
    __tablename__ = "workspace_memory"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)


class ModeratorGrant(Base):
    __tablename__ = "workspace_moderators"
    __table_args__ = (UniqueConstraint("workspace_key", "twitch_user_id", name="uq_workspace_moderator"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    twitch_user_id: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(64), default="Moderator")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChatCommandCooldown(Base):
    __tablename__ = "workspace_chat_cooldowns"
    __table_args__ = (UniqueConstraint("workspace_key", "participant_id", "command", name="uq_chat_command_cooldown"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    participant_id: Mapped[str] = mapped_column(String(96))
    command: Mapped[str] = mapped_column(String(24))
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ClipCooldown(Base):
    __tablename__ = "workspace_clip_cooldowns"
    workspace_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    last_requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OverlayCredential(Base):
    __tablename__ = "workspace_overlay_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BotReview(Base):
    """Explainable suspicious-account review; no automatic enforcement is performed."""
    __tablename__ = "workspace_bot_reviews"
    __table_args__ = (UniqueConstraint("workspace_key", "twitch_user_id", name="uq_workspace_bot_review_user"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    twitch_user_id: Mapped[str] = mapped_column(String(32))
    login: Mapped[str] = mapped_column(String(64), default="")
    display_name: Mapped[str] = mapped_column(String(64), default="Unknown viewer")
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | dismissed | banned
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    official_suspicious_status: Mapped[str] = mapped_column(String(32), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    action_by: Mapped[str] = mapped_column(String(64), default="")
    action_detail: Mapped[str] = mapped_column(String(500), default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)


def _owner(request: Request, db) -> tuple[str, User | None]:
    mode = config.operating_mode()
    if mode == "simulation":
        return "demo", None
    sid = request.cookies.get("qc_session")
    session = db.get(UserSession, sid) if sid else None
    if not session or _now() - session.created_at > timedelta(days=config.SESSION_DAYS):
        raise HTTPException(401, "Connect an authenticated Twitch creator account first")
    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(401, "Creator session is no longer valid")
    return f"twitch:{user.id}", user


def _access(request: Request, db, moderator_ok: bool = False) -> tuple[str, User | None, str]:
    own_key, user = _owner(request, db)
    if user is None:
        return own_key, None, "creator"
    requested_key = request.query_params.get("workspace") or request.headers.get("X-QuestCast-Workspace")
    if requested_key and requested_key != own_key and moderator_ok:
        grant = db.query(ModeratorGrant).filter_by(workspace_key=requested_key, twitch_user_id=user.twitch_id).first()
        if grant:
            return requested_key, user, "moderator"
        raise HTTPException(403, "No moderator grant exists for the requested workspace")
    return own_key, user, "creator"


def _hype_window(db, workspace_key: str) -> SlidingWindow:
    cached = _HYPE_WINDOWS.get(workspace_key)
    if cached is not None:
        return cached
    stream = SlidingWindow(window_s=60, allowed_lateness_s=120)
    cutoff = _now() - timedelta(seconds=180)
    rows = db.query(WorkspaceEvent).filter(WorkspaceEvent.workspace_key == workspace_key,
            WorkspaceEvent.occurred_at >= cutoff).order_by(WorkspaceEvent.occurred_at).all()
    for row in rows:
        stream.ingest({"id": row.id, "type": row.event_type, "at": row.occurred_at,
                       "viewers": json.loads(row.payload_json or "{}").get("viewers")})
    stream.advance(_now())
    _HYPE_WINDOWS[workspace_key] = stream
    return stream


def _hype_state(db, workspace_key: str) -> dict:
    stream = _hype_window(db, workspace_key)
    return stream.active(_now())


def _update_hype(db, workspace_key: str, event: WorkspaceEvent) -> None:
    stream = _hype_window(db, workspace_key)
    payload = json.loads(event.payload_json or "{}")
    stream.ingest({"id": event.id, "type": event.event_type, "at": event.occurred_at,
                   "viewers": payload.get("viewers")}, now=_now())


def _dict_quest(q: Quest, db) -> dict:
    stages = json.loads(q.stages_json or "[]")
    count = db.query(Participation).filter_by(quest_id=q.id).count()
    percent = min(100, round(q.progress / q.target * 100)) if q.target else 0
    active_stage = min(len(stages), int((q.progress / q.target) * len(stages))) if q.target else 0
    stage_progress = [{"name": stage, "status": "completed" if q.status == "completed" or i < active_stage else "active" if i == active_stage else "locked"}
                      for i, stage in enumerate(stages)]
    return {"id": q.id, "title": q.title, "description": q.description, "objective": q.objective,
            "target": q.target, "progress": q.progress, "percent": percent,
            "status": q.status, "stages": stages, "stage_progress": stage_progress, "participants": count,
            "created_at": q.created_at.isoformat() + "Z", "completed_at": q.completed_at.isoformat() + "Z" if q.completed_at else None}


def _event_summary(event_type: str, payload: dict, simulated: bool) -> str:
    who = payload.get("display_name") or payload.get("user_name") or payload.get("chatter_user_name") or "A community member"
    labels = {"channel.follow": "followed the channel", "channel.subscribe": "subscribed", "channel.raid": "arrived with a raid",
              "channel.chat.message": "joined the conversation", "quest.participation": "joined the quest",
              "quest.milestone": "reached a quest milestone", "quest.completed": "completed a quest chapter", "stream.marker": "marked a stream moment", "stream.viewer_sample": "viewer count sampled"}
    prefix = "[DEMO] " if simulated else ""
    return f"{prefix}{who} {labels.get(event_type, event_type)}"


# Transparent deterministic scoring; no AI or opaque model decides facts.
WEIGHTS = {"quest.milestone": 5.0, "quest.completed": 5.0, "stream.marker": 4.0, "channel.raid": 4.0,
           "channel.subscribe": 2.0, "channel.follow": 1.0, "channel.chat.message": 0.3,
           "quest.participation": 0.6}


def _score_window(db, workspace_key: str, event: WorkspaceEvent) -> None:
    start = event.occurred_at - timedelta(seconds=60)
    rows = db.query(WorkspaceEvent).filter(WorkspaceEvent.workspace_key == workspace_key,
            WorkspaceEvent.event_type != "stream.viewer_sample",
            WorkspaceEvent.occurred_at >= start, WorkspaceEvent.occurred_at <= event.occurred_at).order_by(WorkspaceEvent.occurred_at).all()
    points = sum(WEIGHTS.get(r.event_type, 0) for r in rows)
    reasons: dict[str, float] = {}
    for r in rows:
        w = WEIGHTS.get(r.event_type, 0)
        if w:
            reasons[r.event_type] = round(reasons.get(r.event_type, 0) + w, 1)
    if points < 4.0:
        return
    ids = sorted(r.id for r in rows)
    fingerprint = hashlib.sha256((workspace_key + "|" + "|".join(ids)).encode()).hexdigest()
    if db.query(Candidate).filter_by(fingerprint=fingerprint).first():
        return
    first, last = rows[0].occurred_at, rows[-1].occurred_at
    source = "milestone" if any(r.event_type in {"quest.milestone", "quest.completed"} for r in rows) else "streamer_marker" if any(r.event_type == "stream.marker" for r in rows) else "community_activity"
    # Coalesce overlapping hot windows into one review item instead of flooding the queue.
    existing = db.query(Candidate).filter(Candidate.workspace_key == workspace_key,
        Candidate.status == "pending", Candidate.start_at >= event.occurred_at - timedelta(seconds=60),
        Candidate.end_at <= event.occurred_at).order_by(Candidate.created_at.desc()).first()
    if existing:
        existing.fingerprint = fingerprint
        existing.end_at = last
        existing.score = round(points, 1)
        existing.confidence = round(min(.99, points / 12), 2)
        existing.reasons_json = json.dumps(reasons, sort_keys=True)
        if source in {"milestone", "streamer_marker"}: existing.source = source
    else:
        db.add(Candidate(id=secrets.token_hex(16), workspace_key=workspace_key, fingerprint=fingerprint, source=source,
            start_at=first, end_at=last, score=round(points, 1), confidence=round(min(.99, points / 12), 2),
            reasons_json=json.dumps(reasons, sort_keys=True)))


def _add_memory(db, workspace_key: str, kind: str, title: str, body: str, source_id: str | None, simulated: bool):
    db.add(MemoryEntry(id=secrets.token_hex(16), workspace_key=workspace_key, kind=kind, title=title,
                       body=body, source_id=source_id, simulated=simulated))


def _record_event(db, workspace_key: str, event_type: str, payload: dict, source: str, event_id: str, simulated: bool, occurred_at=None):
    if db.get(WorkspaceEvent, event_id):
        return False
    at = occurred_at or _now()
    stored_payload = payload
    if event_type == "channel.chat.message":
        message_text = str((payload.get("message") or {}).get("text") or "")
        normalized = " ".join(message_text.casefold().split())
        stored_payload = {"chatter_user_id": str(payload.get("chatter_user_id") or ""),
                          "chatter_user_name": str(payload.get("chatter_user_name") or "")[:64],
                          "message_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else "",
                          "message_length": min(len(message_text), 500)}
    elif event_type in {"channel.suspicious_user.message", "channel.suspicious_user.update"}:
        stored_payload = {k: payload.get(k) for k in ("user_id", "user_login", "user_name", "low_trust_status",
                          "types", "ban_evasion_evaluation", "shared_ban_channel_ids") if k in payload}
    row = WorkspaceEvent(id=event_id, workspace_key=workspace_key, event_type=event_type, source=source,
                         summary=_event_summary(event_type, payload, simulated), payload_json=json.dumps(stored_payload, default=str),
                         occurred_at=at, simulated=simulated)
    db.add(row)
    _score_window(db, workspace_key, row)
    if event_type in {"quest.milestone", "quest.completed", "stream.marker"}:
        kind = "highlight_marker" if event_type == "stream.marker" else "milestone"
        title = "Streamer marker" if event_type == "stream.marker" else "Quest completed" if event_type == "quest.completed" else "Community milestone"
        _add_memory(db, workspace_key, kind, title, row.summary, event_id, simulated)
    db.commit()
    _update_hype(db, workspace_key, row)
    return True


def _advance_quest(db, quest: Quest, workspace_key: str, old_progress: int, simulated: bool, event_id: str) -> None:
    quest.progress = old_progress + 1
    quest.updated_at = utcnow()
    stages = json.loads(quest.stages_json or "[]")
    for index, stage in enumerate(stages):
        boundary = max(1, (quest.target * (index + 1) + len(stages) - 1) // len(stages))
        if old_progress < boundary <= quest.progress:
            _record_event(db, workspace_key, "quest.milestone", {"display_name": quest.title, "milestone": stage},
                          "system", f"quest-stage:{quest.id}:{index}", simulated)
    if quest.progress >= quest.target and not quest.completed_at:
        quest.status, quest.completed_at = "completed", utcnow()
        db.commit()
        _record_event(db, workspace_key, "quest.completed", {"display_name": quest.title},
                      "system", f"quest-complete:{quest.id}", simulated)
    else:
        db.commit()


def _upsert_bot_review(db, workspace_key: str, twitch_user_id: str, login: str, display_name: str,
                       score: int, evidence: list[dict], official_status: str = "", simulated: bool = False) -> BotReview | None:
    if not twitch_user_id:
        return None
    row = db.query(BotReview).filter_by(workspace_key=workspace_key, twitch_user_id=twitch_user_id).first()
    now = utcnow()
    if row is None:
        row = BotReview(id=secrets.token_hex(16), workspace_key=workspace_key, twitch_user_id=twitch_user_id,
                        login=login[:64], display_name=(display_name or login or "Unknown viewer")[:64],
                        first_seen_at=now, simulated=simulated)
        db.add(row)
    row.login = login[:64] or row.login
    row.display_name = (display_name or login or row.display_name)[:64]
    row.risk_score = max(row.risk_score or 0, min(100, max(0, int(score))))
    row.confidence = round(min(.99, .35 + row.risk_score / 160), 2)
    prior = json.loads(row.evidence_json or "[]")
    merged = {item.get("code"): item for item in prior if isinstance(item, dict) and item.get("code")}
    for item in evidence:
        if item.get("code"):
            merged[item["code"]] = item
    row.evidence_json = json.dumps(list(merged.values())[-12:], sort_keys=True)
    if official_status:
        row.official_suspicious_status = official_status[:32]
    row.last_seen_at = now
    row.updated_at = now
    if row.status == "dismissed" and official_status:
        row.status = "pending"  # a new first-party Twitch signal reopens the review
    db.commit()
    return row


def _evaluate_chat_risk(db, workspace_key: str, payload: dict) -> BotReview | None:
    user_id = str(payload.get("chatter_user_id") or "")
    if not user_id:
        return None
    cutoff = utcnow() - timedelta(seconds=60)
    recent = db.query(WorkspaceEvent).filter(WorkspaceEvent.workspace_key == workspace_key,
        WorkspaceEvent.event_type == "channel.chat.message", WorkspaceEvent.occurred_at >= cutoff).all()
    own = []
    for event in recent:
        try: item = json.loads(event.payload_json or "{}")
        except ValueError: continue
        if str(item.get("chatter_user_id") or "") == user_id:
            own.append(item)
    count = len(own)
    hashes = [x.get("message_hash") for x in own if x.get("message_hash")]
    repeated = max((hashes.count(h) for h in set(hashes)), default=0)
    score, evidence = 0, []
    if count >= 20:
        score += 35; evidence.append({"code": "extreme_burst", "label": f"{count} messages in 60 seconds", "weight": 35})
    elif count >= 10:
        score += 20; evidence.append({"code": "high_burst", "label": f"{count} messages in 60 seconds", "weight": 20})
    if repeated >= 5:
        score += 45; evidence.append({"code": "repeated_content", "label": f"Same message repeated {repeated} times in 60 seconds", "weight": 45})
    elif repeated >= 3:
        score += 30; evidence.append({"code": "repeated_content", "label": f"Same message repeated {repeated} times in 60 seconds", "weight": 30})
    if score < 45:
        return None
    return _upsert_bot_review(db, workspace_key, user_id, str(payload.get("chatter_user_login") or ""),
                              str(payload.get("chatter_user_name") or ""), score, evidence)


def _record_official_suspicious(db, workspace_key: str, event_type: str, payload: dict) -> BotReview | None:
    user_id = str(payload.get("user_id") or "")
    if not user_id:
        return None
    status = str(payload.get("low_trust_status") or "")
    types = payload.get("types") or []
    evaluation = str(payload.get("ban_evasion_evaluation") or "")
    evidence = [{"code": "twitch_suspicious_signal", "label": f"Twitch flagged this account: {status or event_type}", "weight": 70}]
    if types:
        evidence.append({"code": "twitch_signal_types", "label": "Twitch signal types: " + ", ".join(map(str, types))[:180], "weight": 0})
    if evaluation:
        evidence.append({"code": "ban_evasion_evaluation", "label": f"Twitch ban-evasion evaluation: {evaluation}", "weight": 0})
    return _upsert_bot_review(db, workspace_key, user_id, str(payload.get("user_login") or ""),
                              str(payload.get("user_name") or ""), 70, evidence, status)


def _bot_review_dict(row: BotReview) -> dict:
    return {"id": row.id, "twitch_user_id": row.twitch_user_id, "login": row.login,
            "display_name": row.display_name, "risk_score": row.risk_score, "confidence": row.confidence,
            "status": row.status, "evidence": json.loads(row.evidence_json or "[]"),
            "official_suspicious_status": row.official_suspicious_status,
            "first_seen_at": row.first_seen_at.isoformat() + "Z", "last_seen_at": row.last_seen_at.isoformat() + "Z",
            "action_at": row.action_at.isoformat() + "Z" if row.action_at else None,
            "action_by": row.action_by, "action_detail": row.action_detail, "simulated": row.simulated}


def ingest_twitch_event(workspace_key: str, event_type: str, payload: dict, event_id: str, occurred_at: datetime | str | None = None) -> str | None:
    """Called only after a Twitch EventSub notification has been verified and stored."""
    from backend.db import SessionLocal
    try:
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        with SessionLocal() as db:
            inserted = _record_event(db, workspace_key, event_type, payload, "twitch", event_id, False, occurred_at)
            if inserted:
                from backend.expansion import apply_verified_event_progress
                recorded = db.get(WorkspaceEvent, event_id)
                if recorded:
                    apply_verified_event_progress(db, workspace_key, recorded)
            if inserted and event_type == "channel.chat.message":
                _evaluate_chat_risk(db, workspace_key, payload)
                creator_id = None
                if workspace_key.startswith("twitch:"):
                    try:
                        creator = db.get(User, int(workspace_key.split(":", 1)[1]))
                        creator_id = creator.twitch_id if creator else None
                    except (ValueError, IndexError):
                        creator_id = None
                return process_chat_command(db, workspace_key, payload, creator_id)
            elif inserted and event_type in {"channel.suspicious_user.message", "channel.suspicious_user.update"}:
                _record_official_suspicious(db, workspace_key, event_type, payload)
    except Exception:
        import logging
        logging.getLogger("questcast.workspace").exception("Could not process workspace event")
    return None



def recover_interrupted_clip_attempts() -> int:
    """Mark stale processing rows after a backend restart; never invent a clip result."""
    with SessionLocal() as db:
        cutoff = utcnow() - timedelta(minutes=2)
        rows = db.query(Candidate).filter(Candidate.clip_status == "processing", Candidate.clip_requested_at < cutoff).all()
        for candidate in rows:
            candidate.clip_status = "failed"
            candidate.clip_detail = "Backend restarted or the clip workflow exceeded its recovery window. You may retry after the channel cooldown."
        db.commit()
        return len(rows)



def record_viewer_sample(user_id: int, viewer_count: int, sampled_at: datetime | None = None) -> None:
    """Persist a real Helix /streams viewer_count observation (not an EventSub event)."""
    with SessionLocal() as db:
        bucket = int((sampled_at or utcnow()).timestamp() // 60)
        _record_event(db, f"twitch:{user_id}", "stream.viewer_sample", {"viewer_count": max(0, int(viewer_count))},
                      "twitch_helix", f"viewer-sample:{user_id}:{bucket}", False, sampled_at or utcnow())


class QuestCreate(BaseModel):
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(default="", max_length=1000)
    objective: str = Field(default="Community participation", max_length=240)
    target: int = Field(default=25, ge=1, le=100000)
    stages: list[str] = Field(default_factory=lambda: ["Gather", "Build", "Complete"], max_length=8)
    rules: dict[str, Any] = Field(default_factory=lambda: {"one_per_viewer": True})

    @field_validator("stages")
    @classmethod
    def clean_stages(cls, v):
        cleaned = [str(x).strip()[:80] for x in v if str(x).strip()]
        if not cleaned:
            raise ValueError("At least one stage is required")
        return cleaned


class ParticipationIn(BaseModel):
    participant_id: str = Field(min_length=1, max_length=96)
    display_name: str = Field(min_length=1, max_length=64)


class DemoEventIn(BaseModel):
    type: str = Field(pattern=r"^(channel\.follow|channel\.subscribe|channel\.raid|channel\.chat\.message|stream\.marker)$")
    display_name: str = Field(default="DemoViewer", max_length=64)
    count: int = Field(default=1, ge=1, le=20)


class ReviewIn(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")


@router.get("/overview")
def overview(request: Request, db=Depends(get_db)):
    key, user, role = _access(request, db, moderator_ok=True)
    quests = db.query(Quest).filter_by(workspace_key=key).order_by(Quest.created_at.desc()).limit(10).all()
    candidates = db.query(Candidate).filter_by(workspace_key=key).order_by(Candidate.created_at.desc()).limit(12).all()
    events = db.query(WorkspaceEvent).filter_by(workspace_key=key).order_by(WorkspaceEvent.occurred_at.desc()).limit(12).all()
    active = next((q for q in quests if q.status == "active"), None)
    creator = user.display_name if user else "Demo Creator"
    if role == "moderator":
        try:
            owner_id = int(key.split(":", 1)[1])
            owner = db.get(User, owner_id)
            creator = owner.display_name if owner else "Channel creator"
        except (ValueError, IndexError):
            creator = "Channel creator"
    return {"mode": config.operating_mode(), "creator": creator, "role": role,
        "active_quest": _dict_quest(active, db) if active else None,
        "metrics": {"participations": db.query(Participation).join(Quest).filter(Quest.workspace_key == key).count(),
                    "pending_candidates": db.query(Candidate).filter_by(workspace_key=key, status="pending").count(),
                    "milestones": db.query(MemoryEntry).filter_by(workspace_key=key, kind="milestone").count(),
                    "events": db.query(WorkspaceEvent).filter_by(workspace_key=key).count()},
        "candidates": [_candidate(c) for c in candidates], "activity": [_event(e) for e in events],
        "hype": _hype_state(db, key)}


def _candidate(c: Candidate) -> dict:
    return {"id": c.id, "source": c.source, "start_at": c.start_at.isoformat() + "Z", "end_at": c.end_at.isoformat() + "Z",
            "score": c.score, "confidence": c.confidence, "reasons": json.loads(c.reasons_json), "status": c.status,
            "clip_id": c.clip_id, "clip_url": c.clip_url, "clip_status": c.clip_status, "clip_detail": c.clip_detail,
            "title_suggestion": c.title_suggestion or None, "caption_suggestion": c.caption_suggestion or None,
            "simulated": c.workspace_key == "demo"}


def _event(e: WorkspaceEvent) -> dict:
    return {"id": e.id, "type": e.event_type, "source": e.source, "summary": e.summary,
            "at": e.occurred_at.isoformat() + "Z", "simulated": e.simulated}


@router.get("/quests")
def list_quests(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    return [_dict_quest(q, db) for q in db.query(Quest).filter_by(workspace_key=key).order_by(Quest.created_at.desc()).all()]


@router.post("/quests")
def create_quest(body: QuestCreate, request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    q = Quest(id=secrets.token_hex(16), workspace_key=key, title=body.title.strip(), description=body.description.strip(),
              objective=body.objective.strip(), target=body.target, stages_json=json.dumps(body.stages), rules_json=json.dumps(body.rules))
    db.add(q); db.commit(); db.refresh(q)
    return _dict_quest(q, db)


@router.post("/quests/{quest_id}/activate")
def activate_quest(quest_id: str, request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    q = db.query(Quest).filter_by(id=quest_id, workspace_key=key).first()
    if not q: raise HTTPException(404, "Quest not found")
    if q.status == "completed": raise HTTPException(409, "Completed quests cannot be reactivated")
    active = db.query(Quest).filter_by(workspace_key=key, status="active").first()
    if active and active.id != q.id: raise HTTPException(409, "Only one active quest is allowed per channel")
    q.status, q.updated_at = "active", utcnow(); db.commit()
    return _dict_quest(q, db)


@router.post("/quests/{quest_id}/pause")
def pause_quest(quest_id: str, request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    q = db.query(Quest).filter_by(id=quest_id, workspace_key=key).first()
    if not q: raise HTTPException(404, "Quest not found")
    if q.status == "active": q.status = "paused"; q.updated_at = utcnow(); db.commit()
    return _dict_quest(q, db)


@router.post("/quests/{quest_id}/participate")
def participate(quest_id: str, body: ParticipationIn, request: Request, db=Depends(get_db)):
    if config.operating_mode() != "simulation": raise HTTPException(403, "In LIVE mode participation is accepted from verified Twitch chat commands")
    key, _ = _owner(request, db)
    q = db.query(Quest).filter_by(id=quest_id, workspace_key=key, status="active").first()
    if not q: raise HTTPException(404, "No active quest found")
    if db.query(Participation).filter_by(quest_id=q.id, participant_id=body.participant_id).first():
        raise HTTPException(409, "This viewer has already participated in this quest")
    db.add(Participation(quest_id=q.id, participant_id=body.participant_id, display_name=body.display_name.strip()))
    old_progress = q.progress
    _advance_quest(db, q, key, old_progress, True, f"demo-participation:{q.id}:{body.participant_id}")
    _record_event(db, key, "quest.participation", {"display_name": body.display_name}, "simulation", f"demo-participation:{q.id}:{body.participant_id}", True)
    return _dict_quest(q, db)


@router.post("/demo/events")
def demo_event(body: DemoEventIn, request: Request, db=Depends(get_db)):
    if config.operating_mode() != "simulation": raise HTTPException(403, "Demo events are disabled in LIVE mode")
    key, _ = _owner(request, db)
    created = []
    for i in range(body.count):
        eid = f"demo:{secrets.token_hex(12)}"
        payload = {"display_name": body.display_name, "viewers": 12 if body.type == "channel.raid" else None}
        _record_event(db, key, body.type, payload, "simulation", eid, True)
        created.append(eid)
    return {"created": len(created), "event_ids": created, "mode": "simulation"}


@router.get("/candidates")
def list_candidates(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    return [_candidate(c) for c in db.query(Candidate).filter_by(workspace_key=key).order_by(Candidate.created_at.desc()).all()]


@router.post("/candidates/{candidate_id}/review")
def review_candidate(candidate_id: str, body: ReviewIn, request: Request, db=Depends(get_db)):
    key, _, role = _access(request, db, moderator_ok=True)
    c = db.query(Candidate).filter_by(id=candidate_id, workspace_key=key).first()
    if not c: raise HTTPException(404, "Candidate not found")
    if c.status != "pending": raise HTTPException(409, f"Candidate is already {c.status}")
    c.status, c.reviewed_at = body.status, utcnow()
    if body.status == "approved":
        _add_memory(db, key, "approved_highlight", "Creator-approved highlight",
                    f"Approved a {c.source.replace('_', ' ')} candidate with score {c.score}.", c.id, c.workspace_key == "demo")
    db.commit()
    return _candidate(c)


async def _complete_clip_attempt(candidate_id: str, user_id: int) -> None:
    """Background clip creation and Twitch Get Clips confirmation."""
    from backend import twitch as tw
    with SessionLocal() as db:
        candidate = db.get(Candidate, candidate_id)
        user = db.get(User, user_id)
        if not candidate or not user:
            return
        try:
            result = await tw.create_clip_from_vod(user, db, candidate, candidate.title_suggestion or None)
            candidate.clip_status = result.get("status", "unverified")
            candidate.clip_detail = result.get("detail", "")[:500]
            if result.get("status") == "verified":
                candidate.clip_id, candidate.clip_url = result["id"], result["url"]
                candidate.clip_detail = "Twitch returned this clip through Get Clips."
        except tw.TwitchError as exc:
            candidate.clip_status = "failed"
            candidate.clip_detail = f"Twitch API {exc.status}: {exc.message}"[:500]
        except Exception as exc:
            candidate.clip_status = "failed"
            candidate.clip_detail = "Twitch clip workflow failed; check backend logs."
            import logging
            logging.getLogger("questcast.clips").exception("Twitch clip workflow failed")
        db.commit()


@router.post("/candidates/{candidate_id}/clip")
async def clip_candidate(candidate_id: str, request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    key, user = _owner(request, db)
    if config.operating_mode() != "live" or not user: raise HTTPException(403, "Real Twitch clip creation is unavailable in simulation mode")
    if not config.live_ready(): raise HTTPException(503, "LIVE mode is configured but Twitch credentials are incomplete")
    c = db.query(Candidate).filter_by(id=candidate_id, workspace_key=key).first()
    if not c: raise HTTPException(404, "Candidate not found")
    if c.status != "approved": raise HTTPException(409, "Creator approval is required before clipping")
    if not ({"channel:manage:clips", "editor:manage:clips"} & set(user.scopes.split())):
        raise HTTPException(403, "Missing channel:manage:clips or editor:manage:clips scope")
    if c.clip_status == "processing": raise HTTPException(409, "This candidate already has a clip request processing")
    cooldown = db.get(ClipCooldown, key)
    if cooldown and utcnow() - cooldown.last_requested_at < timedelta(seconds=60):
        remaining = max(1, 60 - int((utcnow() - cooldown.last_requested_at).total_seconds()))
        raise HTTPException(429, f"Channel clip cooldown: retry in {remaining}s")
    now = utcnow()
    if cooldown: cooldown.last_requested_at = now
    else: db.add(ClipCooldown(workspace_key=key, last_requested_at=now))
    c.clip_requested_at, c.clip_status, c.clip_detail = now, "processing", "Finding the matching Twitch VOD, creating a timestamp-aligned clip and verifying it with Get Clips."
    db.commit()
    background_tasks.add_task(_complete_clip_attempt, c.id, user.id)
    return {"status": "processing", "candidate_id": c.id,
            "note": "Clip creation was submitted. QuestCast will poll Get Clips and only publish a clip URL after Twitch confirms it."}


@router.get("/candidates/{candidate_id}/clip-status")
def clip_status(candidate_id: str, request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    c = db.query(Candidate).filter_by(id=candidate_id, workspace_key=key).first()
    if not c: raise HTTPException(404, "Candidate not found")
    return {"id": c.id, "status": c.clip_status, "clip_id": c.clip_id, "clip_url": c.clip_url,
            "detail": c.clip_detail,
            "requested_at": c.clip_requested_at.isoformat() + "Z" if c.clip_requested_at else None}


@router.get("/memory")
def memory(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    rows = db.query(MemoryEntry).filter_by(workspace_key=key).order_by(MemoryEntry.occurred_at.desc()).limit(100).all()
    return [{"id": r.id, "kind": r.kind, "title": r.title, "body": r.body,
             "at": r.occurred_at.isoformat() + "Z", "simulated": r.simulated} for r in rows]


@router.post("/overlay/token")
def create_overlay_token(request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    db.query(OverlayCredential).filter_by(workspace_key=key).delete(synchronize_session=False)
    raw = secrets.token_urlsafe(32)
    db.add(OverlayCredential(workspace_key=key, token_hash=hashlib.sha256(raw.encode()).hexdigest()))
    db.commit()
    return {"token": raw, "warning": "Copy this private token into your OBS browser-source URL. It is shown only once."}


@router.delete("/overlay/token")
def revoke_overlay_tokens(request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    removed = db.query(OverlayCredential).filter_by(workspace_key=key).delete(synchronize_session=False)
    db.commit()
    return {"revoked": removed}


@router.get("/overlay/state")
def overlay_state(request: Request, token: str | None = Header(default=None, alias="X-Overlay-Token"), db=Depends(get_db)):
    if config.operating_mode() == "simulation":
        key = "demo"
    else:
        try:
            key, _ = _owner(request, db)
        except HTTPException:
            if not token:
                raise HTTPException(401, "Overlay token required for OBS browser sources")
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            credential = db.query(OverlayCredential).filter_by(token_hash=token_hash).first()
            if not credential:
                raise HTTPException(401, "Invalid overlay token")
            key = credential.workspace_key
    q = db.query(Quest).filter_by(workspace_key=key, status="active").first()
    recent = db.query(WorkspaceEvent).filter_by(workspace_key=key).order_by(WorkspaceEvent.occurred_at.desc()).limit(4).all()
    milestones = db.query(MemoryEntry).filter_by(workspace_key=key, kind="milestone").order_by(MemoryEntry.occurred_at.desc()).limit(3).all()
    from backend.chat_breaks import _active_session, _session_state
    active_break = _session_state(db, _active_session(db, key))
    return {"mode": config.operating_mode(), "quest": _dict_quest(q, db) if q else None,
            "activity": [_event(e) for e in recent],
            "milestones": [{"title": m.title, "body": m.body, "at": m.occurred_at.isoformat() + "Z"} for m in milestones],
            "chat_break": active_break,
            "hype": _hype_state(db, key), "alerts": [{"id": e.id, "summary": e.summary, "at": e.occurred_at.isoformat() + "Z"}
                for e in recent if e.event_type in {"quest.milestone", "quest.completed", "channel.raid", "stream.marker"}]}


@router.get("/leaderboard")
def leaderboard(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    rows = db.query(Participation.participant_id, Participation.display_name, func.count(Participation.id).label("quests")) \
        .join(Quest, Quest.id == Participation.quest_id).filter(Quest.workspace_key == key) \
        .group_by(Participation.participant_id, Participation.display_name).order_by(func.count(Participation.id).desc()).limit(25).all()
    completed = db.query(Quest).filter_by(workspace_key=key, status="completed").all()
    result = []
    for participant_id, display_name, total in rows:
        completed_count = sum(1 for q in completed if db.query(Participation).filter_by(quest_id=q.id, participant_id=participant_id).first())
        badges = (["Quest finisher"] if completed_count else []) + (["Community regular"] if total >= 3 else [])
        result.append({"participant_id": participant_id, "display_name": display_name, "quests_joined": total,
                       "quests_completed": completed_count, "badges": badges})
    return result


@router.get("/analytics")
def analytics(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    events = db.query(WorkspaceEvent).filter_by(workspace_key=key).all()
    heat = [0] * 24
    for event in events:
        heat[event.occurred_at.hour] += 1
    quests = db.query(Quest).filter_by(workspace_key=key).order_by(Quest.created_at.asc()).all()
    funnel = [{"quest": q.title, "created": 1, "participants": db.query(Participation).filter_by(quest_id=q.id).count(),
               "completed": int(q.status == "completed")} for q in quests]
    peaks = []
    for q in quests:
        end = q.completed_at or _now()
        samples = []
        for e in events:
            if q.created_at <= e.occurred_at <= end:
                try: viewers = json.loads(e.payload_json or "{}").get("viewer_count")
                except Exception: viewers = None
                if isinstance(viewers, int): samples.append(viewers)
        peaks.append({"quest": q.title, "peak_viewers": max(samples) if samples else None,
                      "measurement": "observed Twitch Helix viewer_count samples" if samples else "not available from recorded events"})
    return {"event_heatmap_by_hour": heat, "quest_completion_funnel": funnel, "peak_viewers_per_quest": peaks,
            "mode": config.operating_mode()}


@router.get("/moderators")
def list_moderators(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    return [{"twitch_user_id": m.twitch_user_id, "display_name": m.display_name}
            for m in db.query(ModeratorGrant).filter_by(workspace_key=key).order_by(ModeratorGrant.created_at.asc()).all()]


class ModeratorIn(BaseModel):
    twitch_user_id: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=64)


@router.get("/moderator/workspaces")
def moderator_workspaces(request: Request, db=Depends(get_db)):
    _, user = _owner(request, db)
    if not user: return []
    grants = db.query(ModeratorGrant).filter_by(twitch_user_id=user.twitch_id).all()
    out = []
    for grant in grants:
        try: owner = db.get(User, int(grant.workspace_key.split(":", 1)[1]))
        except (ValueError, IndexError): owner = None
        out.append({"workspace": grant.workspace_key, "creator": owner.display_name if owner else "Channel creator"})
    return out


@router.post("/moderators")
def add_moderator(body: ModeratorIn, request: Request, db=Depends(get_db)):
    key, _, role = _access(request, db)
    existing = db.query(ModeratorGrant).filter_by(workspace_key=key, twitch_user_id=body.twitch_user_id).first()
    if existing: raise HTTPException(409, "Moderator already exists")
    db.add(ModeratorGrant(workspace_key=key, twitch_user_id=body.twitch_user_id, display_name=body.display_name.strip()))
    db.commit()
    return {"status": "added", "display_name": body.display_name}


@router.delete("/moderators/{twitch_user_id}")
def remove_moderator(twitch_user_id: str, request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db)
    removed = db.query(ModeratorGrant).filter_by(workspace_key=key, twitch_user_id=twitch_user_id).delete(synchronize_session=False)
    db.commit()
    if not removed: raise HTTPException(404, "Moderator not found")
    return {"status": "removed"}


class StreamMarkerIn(BaseModel):
    description: str = Field(default="QuestCast highlight", max_length=140)


@router.post("/stream-marker")
async def create_workspace_stream_marker(body: StreamMarkerIn, request: Request, db=Depends(get_db)):
    key, user = _owner(request, db)
    if config.operating_mode() != "live" or not user:
        raise HTTPException(409, "Real Twitch stream markers are available only in LIVE mode")
    if "channel:manage:broadcast" not in user.scopes.split():
        raise HTTPException(403, "Reconnect Twitch with channel:manage:broadcast to create stream markers")
    try:
        marker = await tw.create_stream_marker(user, db, body.description)
    except tw.TwitchError as exc:
        raise HTTPException(exc.status, f"Twitch marker request failed: {exc.message}")
    marker_at = marker.get("created_at")
    event_id = str(marker.get("id") or secrets.token_hex(12))
    payload = {"marker_id": event_id, "description": marker.get("description") or body.description,
               "position_seconds": marker.get("position_seconds")}
    ingest_twitch_event(key, "stream.marker", payload, f"stream-marker:{event_id}", marker_at)
    return {"created": True, "marker_id": event_id, "position_seconds": marker.get("position_seconds"),
            "created_at": marker_at, "candidate_note": "The marker is now a recorded event and may contribute to a highlight candidate."}


@router.post("/candidates/{candidate_id}/title-suggestion")
async def candidate_title_suggestion(candidate_id: str, request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    c = db.query(Candidate).filter_by(id=candidate_id, workspace_key=key).first()
    if not c: raise HTTPException(404, "Candidate not found")
    context = f"Source: {c.source}. Event reasons: {json.loads(c.reasons_json)}. Score: {c.score}."
    from backend.ai import generate
    suggestion = await generate("highlight_copy", context)
    title = suggestion.get("suggestion", "A community moment worth remembering").splitlines()[0][:180]
    c.title_suggestion = title
    db.commit()
    return {"title": title, "label": "AI SUGGESTION — REVIEW BEFORE USE", "generated_by": suggestion.get("generated_by", "deterministic_fallback"), "is_suggestion": True}


@router.post("/candidates/{candidate_id}/caption-suggestion")
async def candidate_caption_suggestion(candidate_id: str, request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    c = db.query(Candidate).filter_by(id=candidate_id, workspace_key=key).first()
    if not c: raise HTTPException(404, "Candidate not found")
    context = f"Candidate source: {c.source}. Recorded event signals: {json.loads(c.reasons_json)}. Candidate score: {c.score}. Timestamp: {c.start_at.isoformat()} UTC."
    from backend.ai import generate
    suggestion = await generate("clip_caption", context)
    caption = suggestion.get("suggestion", "A community moment worth remembering")[:1000]
    c.caption_suggestion = caption
    db.commit()
    return {"caption": caption, "label": "AI CAPTION SUGGESTION — REVIEW BEFORE USE",
            "generated_by": suggestion.get("generated_by", "deterministic_fallback"), "is_suggestion": True}


def process_chat_command(db, workspace_key: str, payload: dict, creator_twitch_id: str | None = None) -> str | None:
    message = (payload.get("message") or {}).get("text", "").strip()
    if not message.startswith("!"): return None
    parts = message.split()
    command = parts[0].lower()
    if command == "!vote":
        from backend.chat_breaks import register_vote
        return register_vote(db, workspace_key, payload)
    if command not in {"!join", "!quest", "!progress", "!leaderboard"}: return None
    participant_id = str(payload.get("chatter_user_id") or "")
    display = str(payload.get("chatter_user_name") or "Viewer")[:64]
    if not participant_id: return None
    now = utcnow()
    cooldown = db.query(ChatCommandCooldown).filter_by(workspace_key=workspace_key, participant_id=participant_id, command=command).first()
    if cooldown and now - cooldown.last_used_at < timedelta(seconds=5): return None
    if cooldown: cooldown.last_used_at = now
    else: db.add(ChatCommandCooldown(workspace_key=workspace_key, participant_id=participant_id, command=command, last_used_at=now))
    active = db.query(Quest).filter_by(workspace_key=workspace_key, status="active").first()
    badges = payload.get("badges") or []
    badge_names = {str(b.get("set_id", "")).lower() for b in badges if isinstance(b, dict)}
    is_mod = str(payload.get("chatter_user_id", "")) == str(creator_twitch_id or "") or bool(badge_names & {"moderator", "broadcaster"})
    if command == "!quest" and len(parts) > 1 and parts[1].lower() in {"start", "pause"}:
        if not is_mod:
            reply = "Only channel moderators can control quest state."
        elif parts[1].lower() == "pause":
            if active:
                active.status, active.updated_at = "paused", now
                reply = f"Quest paused: {active.title}."
            else: reply = "There is no active quest to pause."
        else:
            draft = db.query(Quest).filter(Quest.workspace_key == workspace_key, Quest.status.in_(["draft", "paused"])).order_by(Quest.created_at.desc()).first()
            if active: reply = f"A quest is already active: {active.title}."
            elif not draft: reply = "No draft quest is ready to start."
            else:
                draft.status, draft.updated_at = "active", now
                reply = f"Quest started: {draft.title}! Type !join to participate."
    elif command == "!join":
        if not active: reply = "There is no active quest right now."
        elif db.query(Participation).filter_by(quest_id=active.id, participant_id=participant_id).first(): reply = f"{display}, you're already in {active.title}!"
        else:
            db.add(Participation(quest_id=active.id, participant_id=participant_id, display_name=display))
            old = active.progress
            _advance_quest(db, active, workspace_key, old, False, f"chat-participation:{active.id}:{participant_id}")
            _record_event(db, workspace_key, "quest.participation", {"display_name": display}, "twitch", f"chat-participation:{active.id}:{participant_id}", False)
            reply = f"{display} joined {active.title}! {active.progress}/{active.target} progress."
    elif command == "!quest":
        reply = f"Active quest: {active.title} — {active.progress}/{active.target}." if active else "No active quest."
    elif command == "!progress":
        reply = f"{active.title}: {active.progress}/{active.target} ({round(active.progress / active.target * 100)}%)." if active else "No active quest."
    else:
        top = db.query(Participation.display_name, func.count(Participation.id).label("n")).join(Quest).filter(Quest.workspace_key == workspace_key).group_by(Participation.participant_id, Participation.display_name).order_by(func.count(Participation.id).desc()).limit(3).all()
        reply = "Leaderboard: " + (", ".join(f"{i+1}. {row[0]} ({row[1]})" for i, row in enumerate(top)) if top else "No participants yet.")
    db.commit()
    return reply[:450]


class BotDemoSignalIn(BaseModel):
    display_name: str = Field(default="SampleChatter", min_length=1, max_length=64)


@router.post("/bot-guard/demo-signal")
def bot_guard_demo_signal(body: BotDemoSignalIn, request: Request, db=Depends(get_db)):
    key, _ = _owner(request, db)
    if config.operating_mode() != "simulation":
        raise HTTPException(409, "Demo suspicious-account signals are available only in SIMULATION mode")
    user_id = "demo-suspicious-" + hashlib.sha256(body.display_name.casefold().encode()).hexdigest()[:12]
    row = _upsert_bot_review(db, key, user_id, body.display_name.casefold(), body.display_name, 75,
        [{"code": "demo_repeated_content", "label": "[DEMO] Repeated identical chat pattern", "weight": 35},
         {"code": "demo_burst", "label": "[DEMO] High message burst in a short window", "weight": 20},
         {"code": "demo_twitch_signal", "label": "[DEMO] Simulated first-party suspicious signal", "weight": 20}],
        "demo_suspicious", simulated=True)
    return _bot_review_dict(row)


@router.get("/bot-guard/reviews")
def bot_guard_reviews(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    rows = db.query(BotReview).filter_by(workspace_key=key).order_by(BotReview.risk_score.desc(), BotReview.last_seen_at.desc()).limit(100).all()
    return [_bot_review_dict(row) for row in rows]


@router.post("/bot-guard/reviews/{review_id}/dismiss")
def bot_guard_dismiss(review_id: str, request: Request, db=Depends(get_db)):
    key, user, role = _access(request, db, moderator_ok=True)
    row = db.query(BotReview).filter_by(id=review_id, workspace_key=key).first()
    if not row: raise HTTPException(404, "Suspicious-account review not found")
    if row.status == "banned": raise HTTPException(409, "A banned account cannot be dismissed from this review")
    row.status, row.action_at = "dismissed", utcnow()
    row.action_by = user.display_name if user else "Demo Creator"
    row.action_detail = "Marked as reviewed; no Twitch moderation action was taken."
    db.commit()
    return _bot_review_dict(row)


class BotBanIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500, default="Repeated spam behaviour reviewed by the channel creator")


@router.post("/bot-guard/reviews/{review_id}/ban")
async def bot_guard_ban(review_id: str, body: BotBanIn, request: Request, db=Depends(get_db)):
    key, user, role = _access(request, db, moderator_ok=False)
    if not user or config.operating_mode() != "live":
        raise HTTPException(409, "Real Twitch moderation is unavailable in SIMULATION mode")
    if "moderator:manage:banned_users" not in user.scopes.split():
        raise HTTPException(403, "Reconnect Twitch after granting moderator:manage:banned_users")
    row = db.query(BotReview).filter_by(id=review_id, workspace_key=key).first()
    if not row: raise HTTPException(404, "Suspicious-account review not found")
    if row.status == "banned": raise HTTPException(409, "This account is already recorded as banned by QuestCast")
    try:
        result = await tw.helix(user, db, "/moderation/bans",
            {"broadcaster_id": user.twitch_id, "moderator_id": user.twitch_id}, method="POST",
            json={"data": {"user_id": row.twitch_user_id, "reason": body.reason.strip()}})
    except tw.TwitchError as exc:
        if exc.status in {400, 401, 403, 409, 429}:
            raise HTTPException(exc.status, f"Twitch rejected the moderation action: {exc.message}")
        raise HTTPException(502, f"Twitch moderation API error ({exc.status}): {exc.message}")
    confirmed = any(str(item.get("user_id")) == row.twitch_user_id for item in (result.get("data") or []))
    if not confirmed:
        raise HTTPException(502, "Twitch did not return a confirmation for the requested user; QuestCast did not mark the account as banned")
    row.status, row.action_at = "banned", utcnow()
    row.action_by = user.display_name
    row.action_detail = "Twitch accepted the ban request. " + body.reason.strip()[:400]
    db.commit()
    return {**_bot_review_dict(row), "twitch_response": result, "confirmed_by_twitch": True}


@router.post("/bot-guard/reviews/{review_id}/explanation")
async def bot_guard_explanation(review_id: str, request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    row = db.query(BotReview).filter_by(id=review_id, workspace_key=key).first()
    if not row: raise HTTPException(404, "Suspicious-account review not found")
    from backend.ai import generate
    context = json.dumps({"risk_score": row.risk_score, "evidence": json.loads(row.evidence_json or "[]"),
                          "official_twitch_status": row.official_suspicious_status})
    result = await generate("moderation_review", context)
    return {**result, "warning": "AI explanation only. It does not establish that an account is a bot and cannot take moderation action."}


@router.get("/recap")
def recap(request: Request, db=Depends(get_db)):
    """Deterministic, factual recap. Optional AI provider can rewrite this later, never create facts."""
    key, _, _ = _access(request, db, moderator_ok=True)
    events = db.query(WorkspaceEvent).filter_by(workspace_key=key).order_by(WorkspaceEvent.occurred_at.desc()).limit(30).all()
    quests = db.query(Quest).filter_by(workspace_key=key).order_by(Quest.created_at.desc()).limit(5).all()
    lines = [e.summary for e in reversed(events)]
    completed = [q.title for q in quests if q.status == "completed"]
    text = f"QuestCast recorded {len(events)} recent community events. " + ("Completed chapters: " + ", ".join(completed) + ". " if completed else "No quest chapter is marked complete yet. ")
    if lines: text += "Timeline: " + "; ".join(lines[-8:]) + "."
    next_quest = db.query(Quest).filter(Quest.workspace_key == key, Quest.status.in_(["draft", "paused", "active"])) \
        .order_by(Quest.created_at.desc()).first()
    next_objective = {"title": next_quest.title, "objective": next_quest.objective} if next_quest else None
    return {"summary": text, "event_count": len(events), "completed_quests": completed,
            "next_objective": next_objective, "generated_by": "deterministic_fallback", "mode": config.operating_mode()}


class AISuggestionIn(BaseModel):
    kind: str = Field(pattern="^(quest_ideas|highlight_copy|recap_copy)$")
    context: str = Field(default="", max_length=6000)

@router.post("/ai/suggestions")
async def ai_suggestions(body: AISuggestionIn, request: Request, db=Depends(get_db)):
    _owner(request, db)
    from backend.ai import generate
    return await generate(body.kind, body.context)


@router.get("/engagement-coach")
async def engagement_coach_state(request: Request, db=Depends(get_db)):
    _workspace_key, user = _owner(request, db)
    if user is None:
        return {"enabled": False, "mode": "simulation", "provider_configured": False,
                "transport": config.TRANSPORT, "pending_messages": 0, "total_seen": 0,
                "last_report": None, "error": "Connect a Twitch creator account and use LIVE mode to analyze chat."}
    from backend.chat_engagement import snapshot
    return snapshot(user.id)


@router.post("/engagement-coach/start")
async def start_engagement_coach(request: Request, db=Depends(get_db)):
    _workspace_key, user = _owner(request, db)
    if user is None or config.operating_mode() != "live":
        raise HTTPException(409, "The live chat coach requires an authenticated Twitch creator in LIVE mode.")
    if config.TRANSPORT != "websocket":
        raise HTTPException(409, "Live chat coaching requires the Twitch EventSub WebSocket transport.")
    if "user:read:chat" not in user.scopes.split():
        raise HTTPException(403, "Reconnect Twitch and grant user:read:chat before starting chat analysis.")
    from backend.chat_engagement import start
    try:
        return start(user.id)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/engagement-coach/stop")
async def stop_engagement_coach(request: Request, db=Depends(get_db)):
    _workspace_key, user = _owner(request, db)
    if user is None:
        raise HTTPException(409, "The live chat coach is unavailable in simulation mode.")
    from backend.chat_engagement import stop
    return stop(user.id)
