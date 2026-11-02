import asyncio
from datetime import timedelta

from backend import config
from backend.chat_breaks import (
    ChatActivityDraft,
    ChatBreak,
    ChatBreakActivity,
    ChatBreakVote,
    PrepareBreakIn,
    prepare_activities,
    register_vote,
)
from backend.db import Base, SessionLocal, engine, utcnow
from backend.workspace import ChatCommandCooldown

Base.metadata.create_all(engine)


def test_break_prepare_uses_reviewable_curated_fallback(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    result = asyncio.run(prepare_activities(PrepareBreakIn(duration_minutes=6, interval_seconds=180, theme="cozy game")))

    assert result["generated_by"] == "curated_fallback"
    assert len(result["activities"]) == 2
    assert [item["kind"] for item in result["activities"]] == ["poll", "meme"]
    assert len(result["activities"][0]["options"]) == 3
    assert result["activities"][1]["meme_query"]


def test_verified_poll_votes_are_aggregated_without_storing_twitch_user_id():
    workspace = "twitch:break-vote-test"
    now = utcnow()
    session = ChatBreak(id="break-vote-test", workspace_key=workspace, started_at=now,
        ends_at=now + timedelta(minutes=5), interval_seconds=180, next_activity_at=now + timedelta(seconds=180))
    activity = ChatBreakActivity(id="activity-vote-test", break_id=session.id, workspace_key=workspace, position=0,
        kind="poll", title="Test poll", prompt="Pick a side", options_json='["A", "B", "C"]',
        meme_query="", meme_caption="", status="active")
    with SessionLocal() as db:
        db.add_all([session, activity])
        db.commit()
        payload = {"chatter_user_id": "123456789", "message": {"text": "!vote A"}}
        assert register_vote(db, workspace, payload)
        payload["message"]["text"] = "!vote B"
        assert register_vote(db, workspace, payload) is None  # persistent per-viewer cooldown
        cooldown = db.query(ChatCommandCooldown).filter_by(workspace_key=workspace, command="!vote").one()
        cooldown.last_used_at -= timedelta(seconds=6)
        db.commit()
        assert register_vote(db, workspace, payload)

        rows = db.query(ChatBreakVote).filter_by(activity_id=activity.id).all()
        assert len(rows) == 1
        assert rows[0].choice_index == 1
        assert "123456789" not in rows[0].voter_hash
        payload["message"]["text"] = "!vote C"
        payload["chatter_user_id"] = "different-viewer"
        register_vote(db, workspace, payload)
        counts = [db.query(ChatBreakVote).filter_by(activity_id=activity.id, choice_index=i).count() for i in range(3)]
        assert counts == [0, 1, 1]
