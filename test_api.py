"""DB/HTTP tests. Twitch network calls are replaced with fakes: these verify OUR logic, not Twitch itself.
Real-Twitch verification: python -m scripts.verify_twitch"""
import asyncio
import hashlib
import hmac
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import config, health, quests, twitch as tw
from backend.db import Event, SessionLocal, User, UserSession, utcnow
from backend.workspace import BotReview
from backend.main import _events, app

client = TestClient(app)
_ids = itertools.count(1000)


@pytest.fixture
def owner():
    with SessionLocal() as db:
        u = User(twitch_id=str(next(_ids)), login="owner", display_name="Owner")
        db.add(u)
        db.commit()
        return u


def _hook(mtype, body: dict, secret=None, mid="m-1", ts=None):
    raw = json.dumps(body).encode()
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    sig = "sha256=" + hmac.new((secret or config.EVENTSUB_SECRET).encode(), (mid + ts).encode() + raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/twitch", content=raw, headers={
        "Twitch-Eventsub-Message-Id": mid, "Twitch-Eventsub-Message-Timestamp": ts,
        "Twitch-Eventsub-Message-Signature": sig, "Twitch-Eventsub-Message-Type": mtype})


def test_webhook_verification_echoes_challenge():
    r = _hook("webhook_callback_verification", {"challenge": "abc123"})
    assert r.status_code == 200 and r.text == "abc123"


def test_webhook_rejects_bad_signature_and_stale_timestamp():
    assert _hook("notification", {}, secret="wrong").status_code == 403
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    assert _hook("notification", {}, ts=old).status_code == 403


def test_webhook_notification_stored_once_and_updates_health(owner):
    body = {"subscription": {"type": "channel.follow", "condition": {"broadcaster_user_id": owner.twitch_id}},
            "event": {"user_name": "alice"}}
    assert _hook("notification", body, mid="dup-1").status_code == 204
    assert _hook("notification", body, mid="dup-1").status_code == 204  # Twitch retry
    with SessionLocal() as db:
        assert db.query(Event).filter_by(message_id="dup-1").count() == 1
    assert health.get(owner.id).last_event["summary"] == "alice followed"


def test_webhook_duplicate_race_is_acknowledged_not_500(owner, monkeypatch):
    """Two deliveries of one message can both pass the 'already stored?' pre-check; the unique constraint
    then rejects the second insert. That must be acked (204), not surface as a 500 that makes Twitch retry."""
    body = {"subscription": {"type": "channel.follow", "condition": {"broadcaster_user_id": owner.twitch_id}},
            "event": {"user_name": "bob"}}
    assert _hook("notification", body, mid="race-1").status_code == 204
    from sqlalchemy.orm import Query
    real, calls = Query.first, {"n": 0}

    def first(self):  # the pre-check (first Query.first call) cannot see the other worker's row
        calls["n"] += 1
        return None if calls["n"] == 1 else real(self)

    monkeypatch.setattr(Query, "first", first)
    assert _hook("notification", body, mid="race-1").status_code == 204
    monkeypatch.undo()
    with SessionLocal() as db:
        assert db.query(Event).filter_by(message_id="race-1").count() == 1


class _U:
    id, twitch_id = 1, "1"


def test_clip_verified_only_when_get_clips_returns_it(monkeypatch):
    async def helix(user, db, path, params=None, method="GET", json=None):
        return {"data": [{"id": "C1", "edit_url": "e"}]} if method == "POST" else {"data": [{"url": "https://clips.twitch.tv/C1"}]}

    async def nosleep(_): return None
    monkeypatch.setattr(tw, "helix", helix)
    monkeypatch.setattr(tw.asyncio, "sleep", nosleep)
    assert asyncio.run(tw.create_clip(_U(), None))["status"] == "verified"


def test_clip_unverified_when_never_returned(monkeypatch):
    async def helix(user, db, path, params=None, method="GET", json=None):
        return {"data": [{"id": "C1", "edit_url": "e"}]} if method == "POST" else {"data": []}

    async def nosleep(_): return None
    monkeypatch.setattr(tw, "helix", helix)
    monkeypatch.setattr(tw.asyncio, "sleep", nosleep)
    assert asyncio.run(tw.create_clip(_U(), None))["status"] == "unverified"


def test_chat_send_reports_twitch_drop_reason(monkeypatch):
    async def helix(user, db, path, params=None, method="GET", json=None):
        return {"data": [{"message_id": "x", "is_sent": False, "drop_reason": {"code": "msg_rejected"}}]}
    monkeypatch.setattr(tw, "helix", helix)
    r = asyncio.run(tw.send_chat(_U(), None, "hi"))
    assert r["sent"] is False and r["drop_reason"]["code"] == "msg_rejected"


def test_chat_and_clips_require_login():
    assert client.post("/api/chat/send", json={"message": "hi"}).status_code == 401
    assert client.post("/api/clips").status_code == 401


def test_quests_use_only_real_twitch_events(owner):
    with SessionLocal() as db:
        db.add_all([Event(message_id=f"q{owner.id}-{i}", user_id=owner.id, type="channel.follow", source="twitch", payload="{}", created_at=utcnow()) for i in range(3)])
        db.add(Event(message_id=f"q{owner.id}-sim", user_id=owner.id, type="channel.follow", source="simulation", payload="{}", created_at=utcnow()))
        db.commit()
        evs = _events(db, owner, 7)
    assert len(evs) == 3
    assert {q["id"]: q for q in quests.progress(evs, utcnow())}["follower-rush"]["count"] == 3


def test_health_history_persists_only_status_changes(owner):
    health.set_component(owner.id, "chat", status="subscribed")
    health.set_component(owner.id, "chat", status="subscribed")  # no change -> no row
    health.set_component(owner.id, "chat", status="receiving")
    rows = [r for r in health.history(owner.id) if r["kind"] == "chat"]
    assert [r["status"] for r in rows] == ["receiving", "subscribed"]



def test_bot_guard_ban_calls_twitch_only_after_creator_action(owner, monkeypatch):
    monkeypatch.setattr(config, "APP_MODE", "live")
    session_id = "creator-session-botguard"
    with SessionLocal() as db:
        stored_owner = db.get(User, owner.id)
        stored_owner.scopes = "moderator:manage:banned_users"
        review = BotReview(id="bot-review-live", workspace_key=f"twitch:{owner.id}", twitch_user_id="target-user",
            login="spam_account", display_name="SpamAccount", risk_score=70, confidence=.8,
            evidence_json='[{"code":"repeated_content","label":"Repeated messages","weight":45}]')
        db.add(review)
        db.add(UserSession(id=session_id, user_id=owner.id))
        db.commit()
    captured = {}
    async def fake_helix(user, db, path, params=None, method="GET", json=None):
        captured.update(path=path, params=params, method=method, json=json)
        return {"data": [{"user_id": "target-user"}]}
    monkeypatch.setattr(tw, "helix", fake_helix)
    client.cookies.set("qc_session", session_id)
    try:
        response = client.post("/api/workspace/bot-guard/reviews/bot-review-live/ban",
                               json={"reason": "Repeated spam reviewed by creator"})
    finally:
        client.cookies.delete("qc_session")
    assert response.status_code == 200, response.text
    assert response.json()["confirmed_by_twitch"] is True
    assert captured["path"] == "/moderation/bans" and captured["method"] == "POST"
    assert captured["params"] == {"broadcaster_id": owner.twitch_id, "moderator_id": owner.twitch_id}
    assert captured["json"]["data"]["user_id"] == "target-user"


def test_bot_guard_ban_requires_moderation_scope(owner, monkeypatch):
    monkeypatch.setattr(config, "APP_MODE", "live")
    session_id = "creator-session-no-ban-scope"
    with SessionLocal() as db:
        stored_owner = db.get(User, owner.id)
        stored_owner.scopes = "user:read:chat"
        db.add(BotReview(id="bot-review-no-scope", workspace_key=f"twitch:{owner.id}", twitch_user_id="target-user",
            login="target", display_name="Target", risk_score=60, confidence=.7, evidence_json="[]"))
        db.add(UserSession(id=session_id, user_id=owner.id))
        db.commit()
    client.cookies.set("qc_session", session_id)
    try:
        response = client.post("/api/workspace/bot-guard/reviews/bot-review-no-scope/ban",
                               json={"reason": "Repeated spam reviewed by creator"})
    finally:
        client.cookies.delete("qc_session")
    assert response.status_code == 403



def test_create_clip_from_vod_uses_candidate_timestamp_and_verifies(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    class VODUser(_U):
        scopes = "channel:manage:clips"
        twitch_id = "broadcaster-1"
    calls = []
    async def helix(user, db, path, params=None, method="GET", json=None):
        calls.append((path, params, method))
        if path == "/videos":
            return {"data": [{"id": "vod-123", "created_at": "2026-11-02T10:00:00Z", "duration": "30m0s"}]}
        if path == "/videos/clips":
            return {"data": [{"id": "clip-abc", "edit_url": "https://clips.twitch.tv/edit/clip-abc"}]}
        if path == "/clips":
            return {"data": [{"id": "clip-abc", "url": "https://clips.twitch.tv/clip-abc"}]}
        raise AssertionError(path)
    async def no_sleep(_): return None
    monkeypatch.setattr(tw, "helix", helix)
    monkeypatch.setattr(tw.asyncio, "sleep", no_sleep)
    candidate = SimpleNamespace(start_at=datetime(2026,11,2,10,4,50), end_at=datetime(2026,11,2,10,5,10), source="milestone", title_suggestion="The big moment")
    result = asyncio.run(tw.create_clip_from_vod(VODUser(), None, candidate, candidate.title_suggestion))
    assert result["status"] == "verified" and result["url"] == "https://clips.twitch.tv/clip-abc"
    create = next(x for x in calls if x[0] == "/videos/clips")
    assert create[1]["vod_id"] == "vod-123" and create[1]["vod_offset"] == 315
    assert create[1]["duration"] == 30 and create[1]["title"] == "The big moment"


def test_create_clip_from_vod_keeps_candidate_when_matching_vod_missing(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime
    class VODUser(_U):
        scopes = "channel:manage:clips"
        twitch_id = "broadcaster-1"
    async def helix(user, db, path, params=None, method="GET", json=None):
        return {"data": []}
    monkeypatch.setattr(tw, "helix", helix)
    candidate = SimpleNamespace(start_at=datetime(2026,11,2,10,4,50), end_at=datetime(2026,11,2,10,5,10), source="milestone", title_suggestion="")
    with pytest.raises(tw.TwitchError) as exc:
        asyncio.run(tw.create_clip_from_vod(VODUser(), None, candidate))
    assert exc.value.status == 409 and "VOD is available" in exc.value.message



def test_stream_marker_uses_twitch_and_records_real_timestamp(owner, monkeypatch):
    from backend.db import UserSession
    monkeypatch.setattr(config, "APP_MODE", "live")
    session_id = "creator-session-marker"
    with SessionLocal() as db:
        stored_owner = db.get(User, owner.id)
        stored_owner.scopes = "channel:manage:broadcast"
        db.add(UserSession(id=session_id, user_id=owner.id))
        db.commit()
    marker = {"id": "marker-77", "created_at": "2026-11-02T10:00:00Z", "position_seconds": 240, "description": "Quest climax"}
    async def fake_marker(user, db, description):
        assert description == "Quest climax"
        return marker
    monkeypatch.setattr("backend.workspace.tw.create_stream_marker", fake_marker)
    client.cookies.set("qc_session", session_id)
    try:
        response = client.post("/api/workspace/stream-marker", json={"description": "Quest climax"})
    finally:
        client.cookies.delete("qc_session")
    assert response.status_code == 200, response.text
    assert response.json()["marker_id"] == "marker-77"
    from backend.workspace import WorkspaceEvent
    with SessionLocal() as db:
        event = db.query(WorkspaceEvent).filter_by(id="stream-marker:marker-77").one()
        assert event.event_type == "stream.marker" and event.simulated is False
        assert event.occurred_at.isoformat().startswith("2026-11-02T10:00:00")



def test_webhook_persists_chat_fingerprint_not_raw_message(owner):
    payload = {"subscription": {"type": "channel.chat.message", "condition": {"broadcaster_user_id": owner.twitch_id}},
               "event": {"broadcaster_user_id": owner.twitch_id, "chatter_user_id": "viewer-private",
                         "chatter_user_login": "viewer", "chatter_user_name": "Viewer",
                         "message": {"text": "private chat text must not persist", "fragments": [{"type": "text", "text": "private chat text must not persist"}]}}}
    response = _hook("notification", payload, mid="private-chat-1")
    assert response.status_code == 204
    with SessionLocal() as db:
        stored = db.query(Event).filter_by(message_id="private-chat-1").one()
        assert "private chat text must not persist" not in stored.payload
        assert "message_hash" in stored.payload
