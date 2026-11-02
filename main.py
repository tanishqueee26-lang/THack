import asyncio
import json
import logging
import secrets
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from backend import config, health, integration, quests, twitch as tw
from backend.db import Base, Event, User, UserSession, engine, get_db, utcnow
from backend.workspace import Candidate, _candidate, _hype_state
from backend.workspace import router as workspace_router
from backend.expansion import router as expansion_router
from backend.chat_breaks import router as chat_break_router, run_break_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("questcast")
COOKIE = "qc_session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if config.AUTO_CREATE_SCHEMA:
        Base.metadata.create_all(engine)  # local SQLite convenience; deployment uses Alembic
    from backend.workspace import recover_interrupted_clip_attempts
    recovered = recover_interrupted_clip_attempts()
    if recovered:
        log.warning("Marked %s interrupted clip workflow(s) as failed after restart", recovered)
    await integration.start_all()
    break_worker = asyncio.create_task(run_break_scheduler())
    try:
        yield
    finally:
        break_worker.cancel()
        try:
            await break_worker
        except asyncio.CancelledError:
            pass
        integration.stop_all()


app = FastAPI(title="QuestCast API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[origin for origin in [config.FRONTEND_URL, config.TWITCH_EXTENSION_ORIGIN] if origin], allow_credentials=True,
                   allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["*"])
app.include_router(workspace_router, prefix="/api/workspace", tags=["workspace"])
app.include_router(expansion_router, prefix="/api/workspace", tags=["expansion"])
app.include_router(chat_break_router, prefix="/api/workspace", tags=["chat break"])


@app.get("/health")
async def health_check():
    return {"status": "ok"}


# ── Auth (authorization-code flow; the secret never leaves the server) ──────────
def session_user(request: Request, db) -> User | None:
    sid = request.cookies.get(COOKIE)
    s = db.get(UserSession, sid) if sid else None
    if not s or utcnow() - s.created_at > timedelta(days=config.SESSION_DAYS):
        return None
    return db.get(User, s.user_id)


def current_user(request: Request, db=Depends(get_db)) -> User:
    u = session_user(request, db)
    if not u:
        raise HTTPException(401, "Not logged in")
    return u


def _public(u: User) -> dict:
    return {"id": u.twitch_id, "login": u.login, "display_name": u.display_name, "profile_image_url": u.avatar}


@app.get("/auth/login")
async def login():
    if config.operating_mode() != "live" or config.missing_config():
        raise HTTPException(503, f"Twitch login unavailable in {config.operating_mode().upper()} mode; missing: {', '.join(config.missing_config()) or 'none'}")
    state = secrets.token_urlsafe(32)
    resp = RedirectResponse(tw.authorize_url(state))
    resp.set_cookie("qc_oauth_state", state, max_age=600, httponly=True, samesite="lax", secure=config.COOKIE_SECURE)
    return resp


@app.get("/auth/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None, db=Depends(get_db)):
    expected = request.cookies.get("qc_oauth_state", "")
    if error or not code or not state or not secrets.compare_digest(state, expected):
        return RedirectResponse(f"{config.FRONTEND_URL}?auth_error=1")
    tok = await tw.exchange_code(code)
    me = await tw.get_me(tok["access_token"])
    user = db.query(User).filter_by(twitch_id=me["id"]).first() or User(twitch_id=me["id"])
    user.login, user.display_name, user.avatar = me["login"], me["display_name"], me.get("profile_image_url", "")
    tw._apply(user, tok)
    db.add(user)
    db.commit()
    sid = secrets.token_urlsafe(48)
    db.add(UserSession(id=sid, user_id=user.id))
    db.commit()
    await tw.validate_and_store(user, db)
    if config.TRANSPORT == "webhook":
        health.set_component(user.id, "eventsub", status="webhook-idle")
        health.set_component(user.id, "chat", status="unavailable", detail="Chat requires the websocket transport")
        try:
            res = await tw.subscribe_all(user)
            health.get(user.id).eventsub["subscriptions"] = [
                {"type": r.get("type", "-"), "status": f"HTTP {r['status']}" if "status" in r else "skipped",
                 "detail": r.get("skipped", "")} for r in res]
        except Exception as e:
            health.record_error(user.id, "webhook-subscribe", str(e))
    else:
        integration.start(user.id)
    resp = RedirectResponse(config.FRONTEND_URL)
    resp.set_cookie(COOKIE, sid, max_age=config.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=config.COOKIE_SECURE)
    resp.delete_cookie("qc_oauth_state")
    return resp


@app.post("/auth/logout")
async def logout(request: Request, db=Depends(get_db)):
    user = session_user(request, db)
    if user:
        integration.stop(user.id)
        try:
            await tw.revoke(user.access_token)  # best effort: invalidate the token at Twitch
        except Exception as e:
            health.record_error(user.id, "token revoke", str(e))
        user.access_token_enc = user.refresh_token_enc = ""
        user.scopes = ""
        health.get(user.id).token.update(valid=None, checked_at=None)
        s = db.get(UserSession, request.cookies.get(COOKIE, ""))
        if s:
            db.delete(s)
        db.commit()
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
async def me(user: User = Depends(current_user)):
    return _public(user)


# ── Dashboard (real Twitch data + locally stored EventSub events) ──────────────
def _activity(db, user: User) -> list[dict]:
    today = utcnow().date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    counts = dict.fromkeys(days, 0)
    q = db.query(Event.created_at).filter(Event.user_id == user.id, Event.type != "channel.chat.message",
                                          Event.created_at >= datetime.combine(days[0], time.min))
    for (ts,) in q:
        counts[ts.date()] = counts.get(ts.date(), 0) + 1
    return [{"d": d.strftime("%a"), "v": counts[d]} for d in days]


def _events(db, user: User, days: int) -> list[dict]:
    """Real Twitch events only (source == "twitch") as plain dicts for quests/highlights."""
    since = utcnow() - timedelta(days=days)
    rows = db.query(Event).filter(Event.user_id == user.id, Event.source == "twitch", Event.created_at >= since)
    out = []
    for r in rows:
        try:
            viewers = json.loads(r.payload).get("viewers")
        except Exception:
            viewers = None
        out.append({"id": r.message_id, "type": r.type, "at": r.created_at, "viewers": viewers})
    return out


@app.get("/api/dashboard")
async def dashboard(user: User = Depends(current_user), db=Depends(get_db)):
    async def safe(path: str, params: dict):
        try:
            return await tw.helix(user, db, path, params)
        except Exception as e:  # e.g. non-affiliate has no subscriptions endpoint
            log.warning("Helix %s failed: %s", path, e)
            return None

    b = user.twitch_id
    stream = await safe("/streams", {"user_id": b})
    fol = await safe("/channels/followers", {"broadcaster_id": b})
    sub = await safe("/subscriptions", {"broadcaster_id": b})
    clips = await safe("/clips", {"broadcaster_id": b, "first": 5})
    known = stream is not None  # False => Twitch call failed; report unknown, never guess
    live = stream["data"][0] if known and stream["data"] else None
    evs, now = _events(db, user, 7), utcnow()
    stored_candidates = db.query(Candidate).filter_by(workspace_key=f"twitch:{user.id}").order_by(Candidate.created_at.desc()).limit(10).all()
    return {
        "live": bool(live) if known else None,
        "viewers": (live["viewer_count"] if live else 0) if known else None,
        "followers": fol["total"] if fol else None,
        "subscribers": sub["total"] if sub else None,
        "clips": [{"title": c["title"], "url": c["url"], "duration": round(c["duration"]),
                   "view_count": c["view_count"]} for c in (clips or {"data": []})["data"]],
        "activity": _activity(db, user),
        "quests": quests.progress(evs, now),
        "highlights": [_candidate(c) for c in stored_candidates],
        "hype": _hype_state(db, f"twitch:{user.id}"),
    }


@app.post("/api/eventsub/subscribe")
async def resubscribe(user: User = Depends(current_user)):
    return await tw.subscribe_all(user)


# ── EventSub webhook ───────────────────────────────────────────────────────────
@app.post("/webhooks/twitch")
async def twitch_webhook(request: Request, db=Depends(get_db)):
    body, h = await request.body(), request.headers
    mid, ts = h.get("twitch-eventsub-message-id", ""), h.get("twitch-eventsub-message-timestamp", "")
    sig, mtype = h.get("twitch-eventsub-message-signature", ""), h.get("twitch-eventsub-message-type", "")
    if not (mid and ts and sig and config.EVENTSUB_SECRET) or not tw.verify_signature(
            config.EVENTSUB_SECRET, mid, ts, body, sig):
        raise HTTPException(403, "Invalid signature")
    try:  # reject replays older than 10 minutes
        sent = datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(403, "Bad timestamp")
    if abs(datetime.now(timezone.utc) - sent) > timedelta(minutes=10):
        raise HTTPException(403, "Stale message")

    data = json.loads(body)
    if mtype == "webhook_callback_verification":
        return PlainTextResponse(data["challenge"])
    if mtype == "notification":
        if db.query(Event).filter_by(message_id=mid).first():
            return Response(status_code=204)  # duplicate delivery
        sub = data["subscription"]
        cond = sub["condition"]
        owner = db.query(User).filter_by(twitch_id=cond.get("broadcaster_user_id") or cond.get("to_broadcaster_user_id")).first()
        if owner:
            try:
                from backend.privacy import sanitize_twitch_event
                stored_payload = sanitize_twitch_event(sub["type"], data["event"])
                db.add(Event(message_id=mid, user_id=owner.id, type=sub["type"], source="twitch", payload=json.dumps(stored_payload)))
                db.commit()
                from backend.workspace import ingest_twitch_event
                ingest_twitch_event(f"twitch:{owner.id}", sub["type"], data["event"], mid, sent.replace(tzinfo=None))
            except IntegrityError:  # concurrent duplicate delivery won the insert race: already stored, ack it
                db.rollback()
                return Response(status_code=204)
            from backend.integration import summarize
            hh = health.get(owner.id)
            hh.last_event = {"type": sub["type"], "summary": summarize(sub["type"], data["event"]), "at": health.now(), "simulated": False}
            health.set_component(owner.id, "eventsub", status="webhook-active", last_message_at=health.now())
    elif mtype == "revocation":
        log.warning("EventSub revoked: %s", data["subscription"].get("type"))
    return Response(status_code=204)


# ── Integration health & simulation ────────────────────────────────────────────
FEATURE_SCOPES = {"user:write:chat": "Send chat messages", "clips:edit": "Create clips", "channel:read:subscriptions": "Subscriber count + subscribe events",
                  "moderator:read:followers": "Follower count + follow events", "user:read:chat": "Read chat via EventSub",
                  "moderator:read:suspicious_users": "Receive Twitch suspicious-user trust signals",
                  "moderator:manage:banned_users": "Creator-confirmed Twitch bans and unbans",
                  "channel:manage:clips": "Create timestamp-aligned clips from Twitch VODs",
                  "editor:manage:clips": "Create VOD clips as an authorized channel editor",
                  "channel:manage:broadcast": "Create broadcaster stream markers"}


@app.get("/api/integration/status")
async def integration_status(request: Request, db=Depends(get_db)):
    missing = config.missing_config()
    out = {"mode": config.operating_mode(), "simulation_reasons": missing,
           "redirect_uri": config.REDIRECT_URI, "connected": False, "account": None,
           "permissions": {"granted": [], "missing": [{"scope": k, "feature": v} for k, v in FEATURE_SCOPES.items()]},
           "token": {"valid": None, "checked_at": None},
           "eventsub": {"status": "disconnected", "subscriptions": []}, "chat": {"status": "disconnected"},
           "last_event": None, "errors": [], "history": []}
    if config.operating_mode() == "simulation":
        out["last_event"] = health.sim_events[0] if health.sim_events else None
        return out
    user = session_user(request, db) if request.cookies.get(COOKIE) else None
    if not user:
        return out
    h, granted = health.get(user.id), user.scopes.split()
    out.update(connected=h.token["valid"] is True, account=_public(user), token=dict(h.token),
               eventsub=dict(h.eventsub), chat=dict(h.chat), last_event=h.last_event, errors=list(h.errors),
               history=health.history(user.id))
    out["permissions"] = {"granted": granted, "missing": [{"scope": k, "feature": v}
                          for k, v in FEATURE_SCOPES.items() if k not in granted]}
    return out


@app.post("/api/integration/reconnect")
async def reconnect(user: User = Depends(current_user)):
    integration.start(user.id)
    return {"status": "restarting"}


@app.post("/api/clips")
async def create_clip(user: User = Depends(current_user), db=Depends(get_db)):
    if "clips:edit" not in user.scopes.split():
        raise HTTPException(403, "Missing clips:edit scope. Disconnect and reconnect Twitch.")
    try:
        return await tw.create_clip(user, db)  # status is "verified" only if Get Clips returned the clip
    except tw.TwitchError as e:
        raise HTTPException(e.status if e.status in (400, 401, 403, 404, 429) else 502, e.message)


class SimIn(BaseModel):
    type: str


SIM_EVENTS = {"channel.follow": "SimViewer followed", "channel.raid": "SimRaider raided with 42 viewers",
              "stream.online": "Stream went live", "channel.chat.message": "SimViewer sent a chat message"}


@app.post("/api/simulate/event")
async def simulate(body: SimIn):
    if config.operating_mode() != "simulation" or body.type not in SIM_EVENTS:
        raise HTTPException(403, "Simulation is only available in SIMULATION MODE (credentials not configured)")
    ev = {"type": body.type, "summary": "[SIMULATED] " + SIM_EVENTS[body.type], "at": health.now(), "simulated": True}
    health.sim_events.appendleft(ev)  # memory only: never written to the events table
    return ev


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)


_last_chat: dict[int, float] = {}


@app.post("/api/chat/send")
async def send_chat(body: ChatIn, user: User = Depends(current_user), db=Depends(get_db)):
    if "user:write:chat" not in user.scopes.split():
        raise HTTPException(403, "Missing user:write:chat scope. Disconnect and reconnect Twitch.")
    t = _time.monotonic()
    if t - _last_chat.get(user.id, 0) < 1.0:
        raise HTTPException(429, "Slow down: one message per second")
    _last_chat[user.id] = t
    try:
        return await tw.send_chat(user, db, body.message)  # "sent" mirrors Twitch's is_sent; drop_reason if dropped
    except tw.TwitchError as e:
        raise HTTPException(e.status if e.status in (400, 401, 403, 429) else 502, e.message)
