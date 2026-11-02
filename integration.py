"""EventSub WebSocket listener (local development) + token health monitor.
Docs: https://dev.twitch.tv/docs/eventsub/handling-websocket-events/"""
import asyncio
import json
import logging

import websockets
from sqlalchemy.exc import IntegrityError

from backend import config, health, twitch as tw
from backend.db import Event, SessionLocal, User

log = logging.getLogger("questcast.eventsub")
tasks: dict[int, asyncio.Task] = {}
_monitor: asyncio.Task | None = None
_viewer_sampler: asyncio.Task | None = None


def summarize(typ: str, e: dict) -> str:
    return {
        "channel.follow": lambda: f"{e.get('user_name')} followed",
        "channel.subscribe": lambda: f"{e.get('user_name')} subscribed (tier {e.get('tier')})",
        "channel.raid": lambda: f"{e.get('from_broadcaster_user_name')} raided with {e.get('viewers')} viewers",
        "stream.online": lambda: "Stream went live",
        "stream.offline": lambda: "Stream went offline",
        "channel.chat.message": lambda: f"{e.get('chatter_user_name')} sent a chat message",  # text not kept
        "channel.suspicious_user.message": lambda: f"Twitch flagged {e.get('user_name') or e.get('user_login') or 'a viewer'} as suspicious",
        "channel.suspicious_user.update": lambda: f"Twitch updated suspicious status for {e.get('user_name') or e.get('user_login') or 'a viewer'}",
    }.get(typ, lambda: typ)()


async def _subscribe(uid: int, session_id: str) -> None:
    h, results = health.get(uid), []
    with SessionLocal() as db:
        user = db.get(User, uid)
        granted = set(user.scopes.split())
        for typ, ver, scope, cond in tw.SUBSCRIPTIONS:
            if scope and scope not in granted:
                results.append({"type": typ, "status": "skipped", "detail": f"missing scope {scope}"})
                continue
            try:
                st = await tw.create_ws_subscription(user, db, typ, ver, cond(user.twitch_id), session_id)
                results.append({"type": typ, "status": st})
            except tw.TwitchError as e:
                results.append({"type": typ, "status": "error", "detail": f"{e.status}: {e.message}"})
    h.eventsub["subscriptions"] = results
    chat = next(r for r in results if r["type"] == "channel.chat.message")
    ok = chat["status"] == "enabled"
    health.set_component(uid, "chat", status="subscribed" if ok else "unavailable",
                         detail="" if ok else chat.get("detail", chat["status"]))


async def _on_notification(uid: int, message_id: str, payload: dict, message_timestamp: str | None = None) -> None:
    h, typ, ev = health.get(uid), payload["subscription"]["type"], payload["event"]
    is_chat = typ == "channel.chat.message"
    if is_chat:
        health.set_component(uid, "chat", status="receiving", last_message_at=health.now(), messages_seen=h.chat["messages_seen"] + 1)
        # The engagement coach accepts text only while its creator-enabled in-memory worker is running.
        from backend.chat_engagement import ingest
        ingest(uid, (ev.get("message") or {}).get("text", ""), message_id)
    h.last_event = {"type": typ, "summary": summarize(typ, ev), "at": health.now(), "simulated": False}
    from backend.privacy import sanitize_twitch_event
    stored = sanitize_twitch_event(typ, ev)
    with SessionLocal() as db:
        try:
            db.add(Event(message_id=message_id, user_id=uid, type=typ, source="twitch", payload=json.dumps(stored)))
            db.commit()
            from backend.workspace import ingest_twitch_event
            reply = ingest_twitch_event(f"twitch:{uid}", typ, ev, message_id, message_timestamp)
            if reply:
                sender = db.get(User, uid)
                if sender and "user:write:chat" in sender.scopes.split():
                    try:
                        await tw.send_chat(sender, db, reply)
                    except tw.TwitchError as exc:
                        health.record_error(uid, "chat command reply", f"{exc.status}: {exc.message}")
                    except Exception as exc:
                        health.record_error(uid, "chat command reply", f"{type(exc).__name__}: {exc}")
        except IntegrityError:
            db.rollback()  # duplicate delivery


async def _session(uid: int) -> None:
    h = health.get(uid)
    es = h.eventsub
    health.set_component(uid, "eventsub", status="connecting")
    ws = await websockets.connect(config.EVENTSUB_WS_URL)
    timeout, fresh = 15.0, True
    try:
        while True:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            except asyncio.TimeoutError:
                raise RuntimeError("No keepalive/notification within the keepalive window; reconnecting")
            meta, typ = msg["metadata"], msg["metadata"]["message_type"]
            es["last_message_at"] = health.now()
            if typ == "session_welcome":
                sess = msg["payload"]["session"]
                timeout = (sess.get("keepalive_timeout_seconds") or 10) + 5
                health.set_component(uid, "eventsub", status="connected", session_id=sess["id"], connected_at=health.now())
                if fresh:  # a reconnect keeps its subscriptions; only brand-new sessions subscribe (within 10s)
                    await _subscribe(uid, sess["id"])
                fresh = True
            elif typ == "notification":
                await _on_notification(uid, meta["message_id"], msg["payload"], meta.get("message_timestamp"))
            elif typ == "session_reconnect":  # open the new socket first, then close the old one
                new = await websockets.connect(msg["payload"]["session"]["reconnect_url"])
                old, ws = ws, new
                await old.close()
                es["reconnects"] += 1
                fresh = False
            elif typ == "revocation":
                sub = msg["payload"]["subscription"]
                health.record_error(uid, "eventsub", f"{sub['type']} revoked: {sub.get('status')}")
    finally:
        await ws.close()


async def _run(uid: int) -> None:
    h, delay = health.get(uid), 1
    with SessionLocal() as db:
        if not await tw.validate_and_store(db.get(User, uid), db) and h.token["valid"] is False:
            health.set_component(uid, "eventsub", status="auth_error")
            return
    while True:
        try:
            await _session(uid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            health.record_error(uid, "eventsub-websocket", f"{type(e).__name__}: {e}")
        if h.token["valid"] is False:
            health.set_component(uid, "eventsub", status="auth_error")
            return
        health.set_component(uid, "eventsub", status="reconnecting", session_id=None)
        health.set_component(uid, "chat", status="disconnected", detail="EventSub socket down")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


def start(uid: int) -> None:
    stop(uid, stop_engagement=False)
    tasks[uid] = asyncio.create_task(_run(uid))


def stop(uid: int, stop_engagement: bool = True) -> None:
    t = tasks.pop(uid, None)
    if t:
        t.cancel()
    if stop_engagement:
        from backend.chat_engagement import stop as stop_coach
        stop_coach(uid)
    health.set_component(uid, "eventsub", status="disconnected", session_id=None, subscriptions=[])
    health.set_component(uid, "chat", status="disconnected", detail="")


async def _monitor_loop() -> None:
    while True:
        await asyncio.sleep(3600)  # hourly token validation
        with SessionLocal() as db:
            for u in db.query(User).filter(User.access_token_enc != ""):
                await tw.validate_and_store(u, db)


async def _viewer_sampling_loop() -> None:
    """Sample Helix viewer_count once per minute while each authenticated channel is live."""
    while True:
        with SessionLocal() as db:
            users = db.query(User).filter(User.access_token_enc != "").all()
            for user in users:
                try:
                    result = await tw.helix(user, db, "/streams", {"user_id": user.twitch_id})
                    streams = result.get("data", [])
                    if streams:
                        from backend.workspace import record_viewer_sample
                        record_viewer_sample(user.id, int(streams[0].get("viewer_count", 0)))
                except tw.TwitchError as exc:
                    health.record_error(user.id, "viewer sampling", f"{exc.status}: {exc.message}")
                except Exception as exc:
                    health.record_error(user.id, "viewer sampling", f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(60)


async def start_all() -> None:
    global _monitor, _viewer_sampler
    if config.operating_mode() != "live" or config.missing_config():
        return  # explicit simulation mode or incomplete Twitch configuration
    if config.TRANSPORT == "websocket":
        with SessionLocal() as db:
            for u in db.query(User).filter(User.access_token_enc != ""):
                start(u.id)
    _monitor = asyncio.create_task(_monitor_loop())
    _viewer_sampler = asyncio.create_task(_viewer_sampling_loop())


def stop_all() -> None:
    for uid in list(tasks):
        stop(uid)
    from backend.chat_engagement import stop_all as stop_all_coaches
    stop_all_coaches()
    if _monitor:
        _monitor.cancel()
    if _viewer_sampler:
        _viewer_sampler.cancel()
