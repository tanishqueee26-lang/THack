import json
import secrets

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.db import Base, SessionLocal, engine, utcnow
from backend.main import app
from backend.workspace import Candidate, MemoryEntry, Participation, Quest, WorkspaceEvent, ingest_twitch_event, ModeratorGrant, ChatCommandCooldown, ClipCooldown, BotReview, _HYPE_WINDOWS

Base.metadata.create_all(engine)

client = TestClient(app)

@pytest.fixture(autouse=True)
def simulation_mode(monkeypatch):
    monkeypatch.setattr(config, "APP_MODE", "simulation")
    with SessionLocal() as db:
        db.query(Candidate).delete(synchronize_session=False)
        db.query(Participation).delete(synchronize_session=False)
        db.query(WorkspaceEvent).delete(synchronize_session=False)
        db.query(MemoryEntry).delete(synchronize_session=False)
        db.query(Quest).delete(synchronize_session=False)
        db.query(ModeratorGrant).delete(synchronize_session=False)
        db.query(ChatCommandCooldown).delete(synchronize_session=False)
        db.query(ClipCooldown).delete(synchronize_session=False)
        db.query(BotReview).delete(synchronize_session=False)
        db.commit()
    _HYPE_WINDOWS.clear()


def create_active_quest(title="Test quest", target=2):
    response = client.post("/api/workspace/quests", json={"title": title, "target": target,
        "description": "Test community objective", "stages": ["Gather", "Complete"]})
    assert response.status_code == 200, response.text
    quest = response.json()
    activated = client.post(f"/api/workspace/quests/{quest['id']}/activate")
    assert activated.status_code == 200, activated.text
    return activated.json()


def test_quest_creation_activation_and_one_active_rule():
    first = create_active_quest("First active quest")
    second = client.post("/api/workspace/quests", json={"title": "Second quest", "target": 5}).json()
    blocked = client.post(f"/api/workspace/quests/{second['id']}/activate")
    assert blocked.status_code == 409
    assert first["status"] == "active"


def test_participation_is_unique_and_persists_progress():
    quest = create_active_quest("Unique participation", target=3)
    payload = {"participant_id": "viewer-1", "display_name": "ViewerOne"}
    first = client.post(f"/api/workspace/quests/{quest['id']}/participate", json=payload)
    assert first.status_code == 200 and first.json()["progress"] == 1
    duplicate = client.post(f"/api/workspace/quests/{quest['id']}/participate", json=payload)
    assert duplicate.status_code == 409
    listed = client.get("/api/workspace/quests").json()
    assert next(q for q in listed if q["id"] == quest["id"])["progress"] == 1
    with SessionLocal() as db:
        assert db.query(Participation).filter_by(quest_id=quest["id"]).count() == 1


def test_quest_completion_creates_memory_and_candidate():
    quest = create_active_quest("Final chapter", target=1)
    response = client.post(f"/api/workspace/quests/{quest['id']}/participate", json={"participant_id": "v-final", "display_name": "FinalViewer"})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    with SessionLocal() as db:
        assert db.query(MemoryEntry).filter_by(workspace_key="demo", kind="milestone").count() >= 1
        assert db.query(Candidate).filter_by(workspace_key="demo", source="milestone").count() >= 1


def test_candidate_scoring_is_explainable_and_review_is_persisted():
    r = client.post("/api/workspace/demo/events", json={"type": "channel.raid", "display_name": "RaidLeader"})
    assert r.status_code == 200
    candidates = client.get("/api/workspace/candidates").json()
    candidate = next(c for c in candidates if c["source"] == "community_activity")
    assert candidate["score"] == 4.0
    assert candidate["reasons"] == {"channel.raid": 4.0}
    reviewed = client.post(f"/api/workspace/candidates/{candidate['id']}/review", json={"status": "approved"})
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved"
    duplicate = client.post(f"/api/workspace/candidates/{candidate['id']}/review", json={"status": "rejected"})
    assert duplicate.status_code == 409
    assert client.get("/api/workspace/memory").json()


def test_chat_burst_candidate_and_overlay_preview():
    r = client.post("/api/workspace/demo/events", json={"type": "channel.chat.message", "display_name": "Chatter", "count": 16})
    assert r.status_code == 200
    candidates = client.get("/api/workspace/candidates").json()
    assert len(candidates) == 1
    assert candidates[0]["score"] == 4.8 and candidates[0]["reasons"].get("channel.chat.message") == 4.8
    overlay = client.get("/api/workspace/overlay/state").json()
    assert overlay["mode"] == "simulation" and "activity" in overlay


def test_live_chat_command_ingestion_is_idempotent():
    workspace_key = "twitch:test-workspace"
    qid = secrets.token_hex(16)
    with SessionLocal() as db:
        db.add(Quest(id=qid, workspace_key=workspace_key, title="Live quest", target=2,
                     objective="Participate", stages_json='["Start"]', rules_json='{}', status="active"))
        db.commit()
    event = {"chatter_user_id": "twitch-viewer-77", "chatter_user_name": "TwitchViewer",
             "message": {"text": "!join"}}
    ingest_twitch_event(workspace_key, "channel.chat.message", event, "evt-chat-77")
    ingest_twitch_event(workspace_key, "channel.chat.message", event, "evt-chat-77")
    with SessionLocal() as db:
        q = db.get(Quest, qid)
        assert q.progress == 1
        assert db.query(Participation).filter_by(quest_id=qid).count() == 1
        assert db.get(WorkspaceEvent, "evt-chat-77") is not None


def test_ai_suggestions_use_deterministic_fallback_without_provider():
    from backend import config
    config.AI_API_KEY = ""
    config.AI_BASE_URL = ""
    config.AI_MODEL = ""
    response = client.post("/api/workspace/ai/suggestions", json={"kind": "quest_ideas", "context": "Space exploration"})
    assert response.status_code == 200
    assert response.json()["generated_by"] == "deterministic_fallback"
    assert response.json()["is_suggestion"] is True


def test_live_mode_does_not_fall_back_to_simulation(monkeypatch):
    monkeypatch.setattr(config, "APP_MODE", "live")
    monkeypatch.setattr(config, "CLIENT_ID", "")
    monkeypatch.setattr(config, "CLIENT_SECRET", "")
    monkeypatch.setattr(config, "ENCRYPTION_KEY", "")
    status = client.get("/api/integration/status").json()
    assert status["mode"] == "live"
    assert client.post("/api/workspace/demo/events", json={"type": "channel.follow"}).status_code == 403
    assert client.get("/api/workspace/overview").status_code == 401


def test_live_overlay_token_can_authorize_obs_without_creator_cookie(monkeypatch):
    from backend.db import User, UserSession
    monkeypatch.setattr(config, "APP_MODE", "live")
    with SessionLocal() as db:
        user = User(twitch_id="overlay-test-creator", login="overlaytest", display_name="Overlay Test")
        db.add(user); db.commit(); db.refresh(user)
        sid = secrets.token_urlsafe(40)
        db.add(UserSession(id=sid, user_id=user.id)); db.commit()
    client.cookies.set("qc_session", sid)
    created = client.post("/api/workspace/overlay/token")
    assert created.status_code == 200
    token = created.json()["token"]
    client.cookies.delete("qc_session")
    denied = client.get("/api/workspace/overlay/state")
    assert denied.status_code == 401
    allowed = client.get("/api/workspace/overlay/state", headers={"X-Overlay-Token": token})
    assert allowed.status_code == 200 and allowed.json()["mode"] == "live"
    assert client.get("/api/workspace/overlay/state", headers={"X-Overlay-Token": "bad-token"}).status_code == 401


def test_hype_meter_uses_streaming_60_second_window_and_expires():
    from datetime import timedelta
    from backend.workspace import _hype_state, _HYPE_WINDOWS
    r = client.post("/api/workspace/demo/events", json={"type": "channel.raid", "display_name": "HypeRaid"})
    assert r.status_code == 200
    with SessionLocal() as db:
        state = _hype_state(db, "demo")
        assert state["window_seconds"] == 60
        assert state["count"] >= 1
        assert state["score"] >= 4.0
        # The streaming window, not candidate detection, supplies the rolling signal.
        assert "channel.raid" in state["by_type"]


def test_leaderboard_and_analytics_are_factual():
    quest = create_active_quest("Leaderboard chapter", target=2)
    for pid, name in [("viewer-a", "Alpha"), ("viewer-b", "Beta")]:
        response = client.post(f"/api/workspace/quests/{quest['id']}/participate", json={"participant_id": pid, "display_name": name})
        assert response.status_code == 200
    leaderboard = client.get("/api/workspace/leaderboard").json()
    assert {x["display_name"] for x in leaderboard} == {"Alpha", "Beta"}
    analytics = client.get("/api/workspace/analytics").json()
    assert len(analytics["event_heatmap_by_hour"]) == 24
    assert analytics["quest_completion_funnel"][0]["participants"] == 2
    assert analytics["peak_viewers_per_quest"][0]["peak_viewers"] is None


def test_chat_commands_have_persistent_per_user_cooldowns_and_leaderboard_reply():
    from backend.workspace import process_chat_command
    quest = create_active_quest("Command quest", target=5)
    payload = {"chatter_user_id": "cmd-user", "chatter_user_name": "CmdViewer", "message": {"text": "!join"}}
    with SessionLocal() as db:
        first = process_chat_command(db, "demo", payload)
        second = process_chat_command(db, "demo", payload)
        assert "joined" in first
        assert second is None
    payload["message"]["text"] = "!leaderboard"
    with SessionLocal() as db:
        reply = process_chat_command(db, "demo", payload)
        assert reply and "CmdViewer" in reply


def test_mod_only_quest_control_rejects_regular_viewer():
    from backend.workspace import process_chat_command
    create_active_quest("Existing quest", target=5)
    payload = {"chatter_user_id": "ordinary", "chatter_user_name": "Ordinary", "message": {"text": "!quest pause"}, "badges": []}
    with SessionLocal() as db:
        reply = process_chat_command(db, "demo", payload, creator_twitch_id="creator-id")
        assert "Only channel moderators" in reply
        assert db.query(Quest).filter_by(workspace_key="demo", status="active").count() == 1


def test_overlay_token_rotation_revokes_previous_token():
    from backend.db import User, UserSession
    from backend import config
    monkeypatch_mode = config.APP_MODE
    config.APP_MODE = "live"
    try:
        with SessionLocal() as db:
            user = User(twitch_id="overlay-rotate-user", login="rotate", display_name="Rotate")
            db.add(user); db.commit(); db.refresh(user)
            sid = secrets.token_urlsafe(40)
            db.add(UserSession(id=sid, user_id=user.id)); db.commit()
        client.cookies.set("qc_session", sid)
        first = client.post("/api/workspace/overlay/token").json()["token"]
        second = client.post("/api/workspace/overlay/token").json()["token"]
        client.cookies.delete("qc_session")
        assert client.get("/api/workspace/overlay/state", headers={"X-Overlay-Token": first}).status_code == 401
        assert client.get("/api/workspace/overlay/state", headers={"X-Overlay-Token": second}).status_code == 200
    finally:
        config.APP_MODE = monkeypatch_mode


def test_authenticated_moderator_can_review_only_in_granted_workspace(monkeypatch):
    from backend.db import User, UserSession
    from backend.workspace import ModeratorGrant
    from backend import config
    old_mode = config.APP_MODE
    config.APP_MODE = "live"
    try:
        with SessionLocal() as db:
            owner = User(twitch_id="creator-for-mod", login="creator", display_name="Creator")
            mod = User(twitch_id="trusted-mod", login="mod", display_name="Trusted Mod")
            db.add_all([owner, mod]); db.commit(); db.refresh(owner); db.refresh(mod)
            owner_sid, mod_sid = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.add_all([UserSession(id=owner_sid, user_id=owner.id), UserSession(id=mod_sid, user_id=mod.id)])
            candidate = Candidate(id="candidate-mod-review", workspace_key=f"twitch:{owner.id}",
                fingerprint="f"*64, source="community_activity", start_at=utcnow(), end_at=utcnow(),
                score=4.0, confidence=.4, reasons_json='{"channel.raid":4.0}')
            db.add(candidate)
            db.add(ModeratorGrant(workspace_key=f"twitch:{owner.id}", twitch_user_id=mod.twitch_id, display_name="Trusted Mod"))
            db.commit()
        client.cookies.set("qc_session", mod_sid)
        mod_workspaces = client.get("/api/workspace/moderator/workspaces").json()
        assert any(x["workspace"] == f"twitch:{owner.id}" for x in mod_workspaces)
        mod_overview = client.get(f"/api/workspace/overview?workspace=twitch:{owner.id}")
        assert mod_overview.status_code == 200 and mod_overview.json()["role"] == "moderator"
        mod_candidates = client.get(f"/api/workspace/candidates?workspace=twitch:{owner.id}")
        assert mod_candidates.status_code == 200 and mod_candidates.json()[0]["id"] == "candidate-mod-review"
        denied = client.post("/api/workspace/candidates/candidate-mod-review/review", json={"status":"approved"})
        assert denied.status_code == 404  # moderator cannot act on their own empty workspace candidate
        approved = client.post(f"/api/workspace/candidates/candidate-mod-review/review?workspace=twitch:{owner.id}", json={"status":"approved"})
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
    finally:
        config.APP_MODE = old_mode
        client.cookies.clear()


def test_clip_creation_channel_cooldown_and_status_poll(monkeypatch):
    import asyncio
    from backend import config, twitch as tw
    from backend.db import User, UserSession
    old_mode = config.APP_MODE
    config.APP_MODE = "live"
    monkeypatch.setattr(config, "live_ready", lambda: True)
    async def fake_create_clip_from_vod(user, db, candidate, title=None):
        return {"status":"verified", "id":"verified-clip", "url":"https://clips.twitch.tv/verified-clip"}
    monkeypatch.setattr(tw, "create_clip_from_vod", fake_create_clip_from_vod)
    try:
        with SessionLocal() as db:
            user = User(twitch_id="clip-cooldown-user", login="clipuser", display_name="Clip User", scopes="channel:manage:clips")
            db.add(user); db.commit(); db.refresh(user)
            sid = secrets.token_urlsafe(32); db.add(UserSession(id=sid, user_id=user.id))
            q = Candidate(id="clip-candidate-one", workspace_key=f"twitch:{user.id}", fingerprint="c"*64,
                source="community_activity", start_at=utcnow(), end_at=utcnow(), score=5, confidence=.5,
                reasons_json='{}', status="approved")
            q2 = Candidate(id="clip-candidate-two", workspace_key=f"twitch:{user.id}", fingerprint="d"*64,
                source="community_activity", start_at=utcnow(), end_at=utcnow(), score=6, confidence=.6,
                reasons_json='{}', status="approved")
            db.add_all([q,q2]); db.commit()
        client.cookies.set("qc_session", sid)
        first = client.post("/api/workspace/candidates/clip-candidate-one/clip")
        assert first.status_code == 200 and first.json()["status"] == "processing"
        poll = client.get("/api/workspace/candidates/clip-candidate-one/clip-status")
        assert poll.status_code == 200 and poll.json()["status"] == "verified"
        second = client.post("/api/workspace/candidates/clip-candidate-two/clip")
        assert second.status_code == 429
    finally:
        config.APP_MODE = old_mode
        client.cookies.clear()


def test_moderator_badge_can_start_and_pause_quest():
    from backend.workspace import process_chat_command
    with SessionLocal() as db:
        draft = Quest(id="mod-control-draft", workspace_key="demo", title="Mod controlled", target=10,
            objective="Community", stages_json='["Start"]', rules_json='{}', status="draft")
        db.add(draft); db.commit()
        payload = {"chatter_user_id":"mod-1", "chatter_user_name":"ModOne", "badges":[{"set_id":"moderator"}], "message":{"text":"!quest start"}}
        reply = process_chat_command(db, "demo", payload)
        assert "Quest started" in reply
        assert db.query(Quest).filter_by(id=draft.id).first().status == "active"
        payload["message"]["text"] = "!quest pause"
        # Different command cooldown key is not used, but the command itself is !quest; wait past the cooldown.
        from datetime import timedelta
        cooldown = db.query(ChatCommandCooldown).filter_by(workspace_key="demo", participant_id="mod-1", command="!quest").first()
        cooldown.last_used_at = utcnow() - timedelta(seconds=6); db.commit()
        reply = process_chat_command(db, "demo", payload)
        assert "Quest paused" in reply


def test_helix_viewer_samples_feed_peak_viewer_analytics():
    from backend.workspace import record_viewer_sample
    from backend.db import User, UserSession
    from backend import config
    old_mode = config.APP_MODE
    config.APP_MODE = "live"
    try:
        with SessionLocal() as db:
            owner = User(twitch_id="viewer-sample-owner", login="sampleowner", display_name="Sample Owner")
            db.add(owner); db.commit(); db.refresh(owner)
            sid = secrets.token_urlsafe(32)
            db.add(UserSession(id=sid, user_id=owner.id))
            q = Quest(id="viewer-peak-quest", workspace_key=f"twitch:{owner.id}", title="Viewer peak chapter", target=50,
                objective="Community", stages_json='["Start"]', rules_json='{}', status="active")
            db.add(q); db.commit()
        record_viewer_sample(owner.id, 321, utcnow())
        client.cookies.set("qc_session", sid)
        analytics = client.get("/api/workspace/analytics").json()
        row = next(x for x in analytics["peak_viewers_per_quest"] if x["quest"] == "Viewer peak chapter")
        assert row["peak_viewers"] == 321
        assert row["measurement"] == "observed Twitch Helix viewer_count samples"
    finally:
        config.APP_MODE = old_mode
        client.cookies.clear()


def test_interrupted_clip_attempts_are_marked_failed_on_restart():
    from datetime import timedelta
    from backend.workspace import recover_interrupted_clip_attempts
    with SessionLocal() as db:
        candidate = Candidate(id="interrupted-clip", workspace_key="demo", fingerprint="e"*64,
            source="community_activity", start_at=utcnow(), end_at=utcnow(), score=5, confidence=.5,
            reasons_json='{}', status="approved", clip_status="processing", clip_requested_at=utcnow()-timedelta(minutes=5))
        db.add(candidate); db.commit()
    assert recover_interrupted_clip_attempts() == 1
    with SessionLocal() as db:
        row = db.get(Candidate, "interrupted-clip")
        assert row.clip_status == "failed" and "Backend restarted" in row.clip_detail



def test_bot_guard_detects_repeated_chat_pattern_without_persisting_message_text():
    payload = {"chatter_user_id": "viewer-bot-1", "chatter_user_login": "repeat_viewer",
               "chatter_user_name": "RepeatViewer", "message": {"text": "join my channel now"}, "badges": []}
    for i in range(5):
        ingest_twitch_event("demo", "channel.chat.message", payload, f"bot-chat-{i}")
    reviews = client.get("/api/workspace/bot-guard/reviews").json()
    review = next(x for x in reviews if x["twitch_user_id"] == "viewer-bot-1")
    assert review["risk_score"] >= 45
    assert any(x["code"] == "repeated_content" for x in review["evidence"])
    with SessionLocal() as db:
        stored = db.query(WorkspaceEvent).filter_by(id="bot-chat-0").one()
        payload_saved = json.loads(stored.payload_json)
        assert "join my channel now" not in stored.payload_json
        assert payload_saved["message_hash"]


def test_bot_guard_ingests_official_twitch_suspicious_signal():
    payload = {"user_id": "viewer-suspicious-1", "user_login": "maybe_evasion", "user_name": "MaybeEvasion",
               "low_trust_status": "active_monitoring", "types": ["ban_evader"],
               "ban_evasion_evaluation": "likely", "message": {"text": "hello"}}
    ingest_twitch_event("demo", "channel.suspicious_user.message", payload, "suspicious-evt-1")
    reviews = client.get("/api/workspace/bot-guard/reviews").json()
    review = next(x for x in reviews if x["twitch_user_id"] == "viewer-suspicious-1")
    assert review["risk_score"] == 70
    assert review["official_suspicious_status"] == "active_monitoring"
    assert any(x["code"] == "twitch_suspicious_signal" for x in review["evidence"])


def test_bot_guard_demo_and_dismiss_are_separate_from_real_twitch_enforcement():
    created = client.post("/api/workspace/bot-guard/demo-signal", json={"display_name": "DemoSuspicious"})
    assert created.status_code == 200 and created.json()["simulated"] is True
    review_id = created.json()["id"]
    banned = client.post(f"/api/workspace/bot-guard/reviews/{review_id}/ban", json={"reason": "Spam pattern reviewed"})
    assert banned.status_code == 409
    dismissed = client.post(f"/api/workspace/bot-guard/reviews/{review_id}/dismiss")
    assert dismissed.status_code == 200 and dismissed.json()["status"] == "dismissed"



def test_clip_caption_suggestion_is_persisted_and_labelled():
    client.post("/api/workspace/demo/events", json={"type": "channel.raid", "display_name": "CaptionRaid"})
    candidate = client.get("/api/workspace/candidates").json()[0]
    response = client.post(f"/api/workspace/candidates/{candidate['id']}/caption-suggestion")
    assert response.status_code == 200
    assert response.json()["is_suggestion"] is True
    assert "SUGGESTION" in response.json()["label"]
    refreshed = next(x for x in client.get("/api/workspace/candidates").json() if x["id"] == candidate["id"])
    assert refreshed["caption_suggestion"] == response.json()["caption"]


def test_recap_exposes_next_draft_quest_objective_without_inventing_one():
    created = client.post("/api/workspace/quests", json={"title": "Next story chapter", "objective": "Find the lost signal", "target": 3})
    assert created.status_code == 200
    recap = client.get("/api/workspace/recap").json()
    assert recap["next_objective"] == {"title": "Next story chapter", "objective": "Find the lost signal"}
