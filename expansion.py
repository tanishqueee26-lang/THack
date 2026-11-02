"""QuestCast expansion layer: economy, branching stories and guarded integrations.

The local simulation is fully usable. External Twitch Extension, vision, social and
per-game adapters report their configuration state and never pretend to be live.
"""
from __future__ import annotations
import json, secrets, base64
import jwt
from datetime import datetime
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from backend import config
from backend.db import Base, get_db, utcnow, User
from backend.workspace import _access, _owner, WorkspaceEvent, Quest, _advance_quest, _dict_quest

router = APIRouter()

class Wallet(Base):
    __tablename__ = "exp_wallets"
    __table_args__ = (UniqueConstraint("workspace_key", "viewer_id", name="uq_exp_wallet"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    viewer_id: Mapped[str] = mapped_column(String(96))
    display_name: Mapped[str] = mapped_column(String(64), default="Viewer")
    balance: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class EconomyPolicy(Base):
    __tablename__ = "exp_economy_policies"
    workspace_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rules_json: Mapped[str] = mapped_column(Text, default='{}')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class Ledger(Base):
    __tablename__ = "exp_economy_ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    viewer_id: Mapped[str] = mapped_column(String(96), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(160))
    reference: Mapped[str] = mapped_column(String(96), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class Inventory(Base):
    __tablename__ = "exp_inventory"
    __table_args__ = (UniqueConstraint("workspace_key", "viewer_id", "item_key", name="uq_exp_inventory"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    viewer_id: Mapped[str] = mapped_column(String(96), index=True)
    item_key: Mapped[str] = mapped_column(String(48))
    quantity: Mapped[int] = mapped_column(Integer, default=0)

class Trade(Base):
    __tablename__ = "exp_trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    from_viewer: Mapped[str] = mapped_column(String(96))
    to_viewer: Mapped[str] = mapped_column(String(96))
    item_key: Mapped[str] = mapped_column(String(48))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class StoryNode(Base):
    __tablename__ = "exp_story_nodes"
    __table_args__ = (UniqueConstraint("workspace_key", "story_key", name="uq_exp_story_node"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    story_key: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(120))
    prompt: Mapped[str] = mapped_column(Text, default="")
    choices_json: Mapped[str] = mapped_column(Text, default="[]")
    selected_choice: Mapped[str] = mapped_column(String(48), default="")
    parent_node_id: Mapped[str | None] = mapped_column(ForeignKey("exp_story_nodes.id", ondelete="SET NULL"), nullable=True)
    parent_choice_key: Mapped[str] = mapped_column(String(48), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class StoryVote(Base):
    __tablename__ = "exp_story_votes"
    __table_args__ = (UniqueConstraint("node_id", "viewer_id", name="uq_exp_story_vote"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("exp_story_nodes.id", ondelete="CASCADE"), index=True)
    viewer_id: Mapped[str] = mapped_column(String(96))
    choice_key: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class IntegrationJob(Base):
    __tablename__ = "exp_integration_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True) # vision | publish | gameplay
    status: Mapped[str] = mapped_column(String(32), default="queued")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    detail: Mapped[str] = mapped_column(String(500), default="")
    creator_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class ProgressProposal(Base):
    __tablename__ = "exp_progress_proposals"
    __table_args__ = (UniqueConstraint("workspace_key", "event_id", name="uq_exp_progress_event"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(96), index=True)
    quest_id: Mapped[str] = mapped_column(String(36))
    event_id: Mapped[str] = mapped_column(String(96))
    points: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

# Virtual items are creator-configurable later; this safe starter catalogue is non-cash and non-transferable by default.
CATALOG = {
    "spotlight": {"name": "Spotlight", "cost": 40, "description": "A temporary overlay shout-out"},
    "confetti": {"name": "Confetti Burst", "cost": 25, "description": "A celebratory overlay effect"},
    "reroll": {"name": "Quest Reroll", "cost": 60, "description": "Vote to reroll a community choice"},
}
ALLOWED_EVENT_POINTS = {"channel.follow": 1, "channel.subscribe": 3, "channel.raid": 5, "quest.participation": 1, "quest.milestone": 2}

class ViewerIn(BaseModel):
    viewer_id: str = Field(min_length=1, max_length=96)
    display_name: str = Field(default="Viewer", max_length=64)
class AwardIn(ViewerIn):
    amount: int = Field(ge=1, le=500)
    reason: str = Field(min_length=2, max_length=160)
class EconomyPolicyIn(BaseModel):
    enabled: bool
    event_rewards: dict[str, int] = Field(default_factory=dict)
class PurchaseIn(ViewerIn):
    item_key: str = Field(min_length=1, max_length=48)
class TradeIn(BaseModel):
    from_viewer: str = Field(min_length=1, max_length=96)
    to_viewer: str = Field(min_length=1, max_length=96)
    item_key: str = Field(min_length=1, max_length=48)
    quantity: int = Field(ge=1, le=20)
class StoryCreate(BaseModel):
    story_key: str = Field(min_length=1, max_length=48)
    title: str = Field(min_length=2, max_length=120)
    prompt: str = Field(default="", max_length=1200)
    choices: list[dict[str, str]] = Field(min_length=2, max_length=5)
    parent_node_id: str | None = None
    parent_choice_key: str = Field(default="", max_length=48)
class VoteIn(BaseModel):
    viewer_id: str = Field(min_length=1, max_length=96)
    choice_key: str = Field(min_length=1, max_length=48)
class VisionIn(BaseModel):
    vod_url: str = Field(min_length=8, max_length=1000)
    consent_confirmed: bool
class PublishIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    platform: str = Field(pattern="^(youtube|instagram|tiktok)$")
    caption: str = Field(default="", max_length=2200)
class GameplayIn(BaseModel):
    adapter: str = Field(pattern="^(sandbox|custom)$")
    action: str = Field(pattern="^(show_hint|announce_milestone|trigger_safe_event)$")
    payload: dict[str, Any] = Field(default_factory=dict)
class ProposalIn(BaseModel):
    quest_id: str
    event_id: str
class ProgressPolicyIn(BaseModel):
    quest_id: str
    enabled: bool
    event_types: list[str] = Field(default_factory=list, max_length=8)


def _workspace(request: Request, db, moderator_ok: bool = False):
    key, user, role = _access(request, db, moderator_ok=moderator_ok)
    if role != "creator":
        raise HTTPException(403, "Creator permission is required for this action")
    return key, user

def _wallet(db, key: str, viewer_id: str, display_name: str = "Viewer") -> Wallet:
    w = db.query(Wallet).filter_by(workspace_key=key, viewer_id=viewer_id).first()
    if not w:
        w = Wallet(workspace_key=key, viewer_id=viewer_id, display_name=display_name[:64], balance=0)
        db.add(w); db.flush()
    return w

def _ledger(db, key, viewer, amount, reason, reference=""):
    db.add(Ledger(id=secrets.token_hex(16), workspace_key=key, viewer_id=viewer, amount=amount, reason=reason[:160], reference=reference[:96]))

@router.get("/expansion/summary")
def expansion_summary(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    jobs = db.query(IntegrationJob).filter_by(workspace_key=key).order_by(IntegrationJob.created_at.desc()).limit(10).all()
    return {"mode": config.operating_mode(), "economy": {"currency": "Quest Coins", "cash_value": False, "catalog": CATALOG},
      "story_nodes": db.query(StoryNode).filter_by(workspace_key=key).count(),
      "wallets": db.query(Wallet).filter_by(workspace_key=key).count(),
      "jobs": [{"id": j.id, "kind": j.kind, "status": j.status, "detail": j.detail} for j in jobs],
      "capabilities": {"economy": "available_in_simulation_and_live", "branching_story": "available_in_simulation_and_live",
        "twitch_extension": "developer_console_setup_required", "vision": "provider_and_ffmpeg_required",
        "social_publish": "platform_oauth_and_review_required", "gameplay": "game_adapter_required",
        "ai_progress": "recorded_event_evidence_and_creator_approval"}}

@router.get("/expansion/assistant-state")
def assistant_state(request: Request, db=Depends(get_db)):
    """Return only workspace-scoped quests, eligible recorded events and reviewable proposals."""
    key, _ = _workspace(request, db)
    quests = db.query(Quest).filter_by(workspace_key=key, status="active").order_by(Quest.created_at.desc()).all()
    events = (db.query(WorkspaceEvent).filter_by(workspace_key=key)
              .filter(WorkspaceEvent.event_type.in_(ALLOWED_EVENT_POINTS.keys()))
              .order_by(WorkspaceEvent.occurred_at.desc()).limit(40).all())
    proposals = (db.query(ProgressProposal).filter_by(workspace_key=key, status="pending")
                 .order_by(ProgressProposal.created_at.desc()).limit(40).all())
    quest_by_id = {q.id: q for q in quests}
    event_ids = {p.event_id for p in proposals}
    event_by_id = {e.id: e for e in db.query(WorkspaceEvent).filter(
        WorkspaceEvent.workspace_key == key, WorkspaceEvent.id.in_(event_ids)).all()} if event_ids else {}
    return {
        "quests": [{"id": q.id, "title": q.title, "progress": q.progress, "target": q.target} for q in quests],
        "events": [{"id": e.id, "event_type": e.event_type, "summary": e.summary,
                    "source": e.source, "occurred_at": e.occurred_at.isoformat(), "simulated": e.simulated,
                    "points": ALLOWED_EVENT_POINTS[e.event_type]} for e in events],
        "proposals": [{"id": p.id, "quest_id": p.quest_id, "quest_title": quest_by_id[p.quest_id].title if p.quest_id in quest_by_id else "Quest",
                       "event_id": p.event_id, "event_type": event_by_id[p.event_id].event_type if p.event_id in event_by_id else "recorded event",
                       "event_summary": event_by_id[p.event_id].summary if p.event_id in event_by_id else "Recorded evidence",
                       "simulated": event_by_id[p.event_id].simulated if p.event_id in event_by_id else True,
                       "points": p.points, "reason": p.reason, "created_at": p.created_at.isoformat()} for p in proposals]
    }

@router.get("/expansion/economy/{viewer_id}")
def economy_wallet(viewer_id: str, request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    w = _wallet(db, key, viewer_id)
    inv = db.query(Inventory).filter_by(workspace_key=key, viewer_id=viewer_id).all()
    db.commit()
    earned = sum(max(0, row.amount) for row in db.query(Ledger).filter_by(workspace_key=key, viewer_id=viewer_id).all())
    return {"viewer_id": viewer_id, "balance": w.balance, "level": 1 + earned // 100, "xp": earned % 100,
      "inventory": [{"item_key": x.item_key, "quantity": x.quantity, "name": CATALOG.get(x.item_key, {}).get("name", x.item_key)} for x in inv]}

@router.post("/expansion/economy/policy")
def economy_policy(body: EconomyPolicyIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    allowed = {"channel.follow", "channel.subscribe", "channel.subscription.gift"}
    if set(body.event_rewards) - allowed or any(v < 0 or v > 100 for v in body.event_rewards.values()):
        raise HTTPException(422, "Rewards may only be configured for supported verified events, from 0 to 100 coins")
    row = db.get(EconomyPolicy, key)
    if not row: row = EconomyPolicy(workspace_key=key, enabled=body.enabled, rules_json=json.dumps(body.event_rewards)); db.add(row)
    else: row.enabled=body.enabled; row.rules_json=json.dumps(body.event_rewards); row.updated_at=utcnow()
    db.commit()
    return {"enabled":row.enabled,"event_rewards":json.loads(row.rules_json),"note":"Only verified, non-simulated Twitch events can earn coins. Coins have no cash value."}

@router.post("/expansion/economy/award")
def economy_award(body: AwardIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    if config.operating_mode() != "simulation":
        raise HTTPException(403, "Live coin awards must come from verified Twitch events or a creator-configured reward rule")
    w = _wallet(db, key, body.viewer_id, body.display_name); w.balance += body.amount; w.updated_at = utcnow()
    _ledger(db, key, body.viewer_id, body.amount, body.reason)
    db.commit()
    return {"viewer_id": body.viewer_id, "balance": w.balance, "awarded": body.amount, "simulated": True}

@router.post("/expansion/economy/purchase")
def economy_purchase(body: PurchaseIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    item = CATALOG.get(body.item_key)
    if not item: raise HTTPException(404, "Unknown item")
    w = _wallet(db, key, body.viewer_id, body.display_name)
    if w.balance < item["cost"]: raise HTTPException(409, "Not enough Quest Coins")
    w.balance -= item["cost"]
    inv = db.query(Inventory).filter_by(workspace_key=key, viewer_id=body.viewer_id, item_key=body.item_key).first()
    if not inv: inv = Inventory(workspace_key=key, viewer_id=body.viewer_id, item_key=body.item_key, quantity=0); db.add(inv)
    inv.quantity += 1; _ledger(db, key, body.viewer_id, -item["cost"], f"Purchased {item['name']}")
    db.commit(); return {"balance": w.balance, "item": body.item_key, "quantity": inv.quantity}

@router.post("/expansion/economy/trades")
def trade_create(body: TradeIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    if body.from_viewer == body.to_viewer: raise HTTPException(400, "Cannot trade with yourself")
    if body.item_key not in CATALOG: raise HTTPException(404, "Unknown item")
    inv = db.query(Inventory).filter_by(workspace_key=key, viewer_id=body.from_viewer, item_key=body.item_key).first()
    if not inv or inv.quantity < body.quantity: raise HTTPException(409, "Insufficient inventory")
    t = Trade(id=secrets.token_hex(16), workspace_key=key, from_viewer=body.from_viewer, to_viewer=body.to_viewer, item_key=body.item_key, quantity=body.quantity)
    db.add(t); db.commit(); return {"trade_id": t.id, "status": t.status, "note": "Items remain with the sender until accepted"}

@router.post("/expansion/economy/trades/{trade_id}/accept")
def trade_accept(trade_id: str, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    t = db.query(Trade).filter_by(id=trade_id, workspace_key=key).first()
    if not t: raise HTTPException(404, "Trade not found")
    if t.status != "pending": raise HTTPException(409, "Trade is no longer pending")
    inv = db.query(Inventory).filter_by(workspace_key=key, viewer_id=t.from_viewer, item_key=t.item_key).first()
    if not inv or inv.quantity < t.quantity: raise HTTPException(409, "Sender no longer owns the offered quantity")
    inv.quantity -= t.quantity
    target = db.query(Inventory).filter_by(workspace_key=key, viewer_id=t.to_viewer, item_key=t.item_key).first()
    if not target: target = Inventory(workspace_key=key, viewer_id=t.to_viewer, item_key=t.item_key, quantity=0); db.add(target)
    target.quantity += t.quantity; t.status = "accepted"; db.commit()
    return {"trade_id": t.id, "status": t.status}

@router.post("/expansion/economy/use")
def economy_use(body: PurchaseIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    inv = db.query(Inventory).filter_by(workspace_key=key, viewer_id=body.viewer_id, item_key=body.item_key).first()
    if not inv or inv.quantity < 1: raise HTTPException(409, "This viewer does not own that ability")
    if body.item_key == "reroll":
        job = IntegrationJob(id=secrets.token_hex(16), workspace_key=key, kind="ability", status="awaiting_creator_review",
            payload_json=json.dumps({"ability":"reroll","viewer_id":body.viewer_id}), detail="Viewer requested a story-vote reroll; creator must approve the new vote")
        db.add(job)
        effect = "Reroll request sent to creator review"
    elif body.item_key in {"spotlight", "confetti"}:
        from backend.workspace import _record_event
        event_id = f"ability:{secrets.token_hex(12)}"
        _record_event(db, key, "economy.ability_used", {"display_name": body.display_name, "ability": body.item_key},
                      "simulation" if config.operating_mode() == "simulation" else "creator_action", event_id,
                      config.operating_mode() == "simulation")
        job = IntegrationJob(id=secrets.token_hex(16), workspace_key=key, kind="ability", status="triggered",
            payload_json=json.dumps({"ability":body.item_key,"viewer_id":body.viewer_id}), detail="Overlay ability event recorded")
        db.add(job)
        effect = "Overlay ability event recorded"
    else:
        raise HTTPException(422, "Unknown ability")
    inv.quantity -= 1
    db.commit()
    return {"ability":body.item_key,"remaining":inv.quantity,"effect":effect}

@router.post("/expansion/story/nodes")
def story_create(body: StoryCreate, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    if len({c.get("key") for c in body.choices}) != len(body.choices) or any(not c.get("key") or not c.get("label") for c in body.choices):
        raise HTTPException(422, "Each story choice needs a unique key and label")
    if db.query(StoryNode).filter_by(workspace_key=key, story_key=body.story_key).first(): raise HTTPException(409, "Story node key already exists")
    if body.parent_node_id:
        parent = db.query(StoryNode).filter_by(id=body.parent_node_id, workspace_key=key, status="locked").first()
        if not parent or not body.parent_choice_key or parent.selected_choice != body.parent_choice_key:
            raise HTTPException(409, "A child chapter must follow the branch selected by its locked parent")
    elif body.parent_choice_key:
        raise HTTPException(422, "parent_choice_key requires parent_node_id")
    n = StoryNode(id=secrets.token_hex(16), workspace_key=key, story_key=body.story_key, title=body.title, prompt=body.prompt, choices_json=json.dumps(body.choices), status="voting", parent_node_id=body.parent_node_id, parent_choice_key=body.parent_choice_key)
    db.add(n); db.commit(); return {"id": n.id, "story_key": n.story_key, "title": n.title, "choices": body.choices, "status": n.status, "parent_node_id": n.parent_node_id, "parent_choice_key": n.parent_choice_key}

@router.get("/expansion/story")
def story_list(request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    out=[]
    for n in db.query(StoryNode).filter_by(workspace_key=key).order_by(StoryNode.created_at.desc()).all():
        votes=db.query(StoryVote).filter_by(node_id=n.id).all(); tally={c["key"]:0 for c in json.loads(n.choices_json)}
        for v in votes: tally[v.choice_key]=tally.get(v.choice_key,0)+1
        out.append({"id":n.id,"story_key":n.story_key,"title":n.title,"prompt":n.prompt,"choices":json.loads(n.choices_json),"tally":tally,"selected_choice":n.selected_choice,"status":n.status,"parent_node_id":n.parent_node_id,"parent_choice_key":n.parent_choice_key})
    return out

@router.post("/expansion/story/{node_id}/vote")
def story_vote(node_id: str, body: VoteIn, request: Request, db=Depends(get_db)):
    key, _, _ = _access(request, db, moderator_ok=True)
    n=db.query(StoryNode).filter_by(id=node_id,workspace_key=key,status="voting").first()
    if not n: raise HTTPException(404,"Open story vote not found")
    if body.choice_key not in {c["key"] for c in json.loads(n.choices_json)}: raise HTTPException(422,"Invalid story choice")
    existing=db.query(StoryVote).filter_by(node_id=n.id,viewer_id=body.viewer_id).first()
    if existing: existing.choice_key=body.choice_key
    else: db.add(StoryVote(node_id=n.id,viewer_id=body.viewer_id,choice_key=body.choice_key))
    db.commit(); return {"accepted":True,"note":"One vote per viewer; viewers may change their vote until the creator locks the branch"}

@router.post("/expansion/story/{node_id}/lock")
def story_lock(node_id: str, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    n=db.query(StoryNode).filter_by(id=node_id,workspace_key=key,status="voting").first()
    if not n: raise HTTPException(404,"Open story vote not found")
    votes=db.query(StoryVote).filter_by(node_id=n.id).all(); tally={c["key"]:0 for c in json.loads(n.choices_json)}
    for v in votes: tally[v.choice_key]=tally.get(v.choice_key,0)+1
    if not votes: raise HTTPException(409,"No community votes to resolve")
    highest=max(tally.values()); winners=sorted(k for k,v in tally.items() if v==highest)
    if len(winners)>1: raise HTTPException(409,"Vote is tied; creator must choose a branch explicitly")
    n.selected_choice=winners[0]; n.status="locked"; db.commit()
    return {"story_key":n.story_key,"selected_choice":n.selected_choice,"tally":tally,"next_step":"Creator can create a follow-up node with this branch as its parent in the story graph"}

@router.post("/expansion/vision/jobs")
def vision_job(body: VisionIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    if not body.consent_confirmed: raise HTTPException(400,"Creator consent is required before analysing stream video")
    # No hidden downloads or fake analysis: a job is queued only when a vision provider is configured.
    job=IntegrationJob(id=secrets.token_hex(16),workspace_key=key,kind="vision",status="blocked_unconfigured",
      payload_json=json.dumps({"vod_url":body.vod_url}),detail="Frame-sampling worker and multimodal vision provider are not configured. No video was fetched or analysed.")
    db.add(job); db.commit(); return {"job_id":job.id,"status":job.status,"detail":job.detail}

@router.post("/expansion/publish")
def publish_job(body: PublishIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    from backend.workspace import Candidate
    c=db.query(Candidate).filter_by(id=body.candidate_id,workspace_key=key,status="approved").first()
    if not c or not c.clip_url: raise HTTPException(409,"Only a creator-approved candidate with a verified clip URL can enter publishing")
    job=IntegrationJob(id=secrets.token_hex(16),workspace_key=key,kind="publish",status="awaiting_platform_oauth",
      payload_json=json.dumps({"candidate_id":c.id,"platform":body.platform,"caption":body.caption}),detail="Creator approval is recorded; platform OAuth/API credentials and a publishing worker are not configured.",creator_approved=True)
    db.add(job); db.commit(); return {"job_id":job.id,"status":job.status,"detail":job.detail,"published":False}

@router.post("/expansion/gameplay/propose")
def gameplay_propose(body: GameplayIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    if body.adapter == "custom":
        job=IntegrationJob(id=secrets.token_hex(16),workspace_key=key,kind="gameplay",status="blocked_adapter_missing",payload_json=json.dumps({"action":body.action,"payload":body.payload}),detail="No game-specific adapter is installed. Nothing was sent to a game.")
    else:
        job=IntegrationJob(id=secrets.token_hex(16),workspace_key=key,kind="gameplay",status="simulated",payload_json=json.dumps({"action":body.action,"payload":body.payload}),detail="Sandbox action recorded only; no gameplay process was controlled.")
    db.add(job); db.commit(); return {"job_id":job.id,"status":job.status,"detail":job.detail}

@router.post("/expansion/ai-progress/policy")
def ai_progress_policy(body: ProgressPolicyIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    quest = db.query(Quest).filter_by(id=body.quest_id, workspace_key=key).first()
    if not quest: raise HTTPException(404, "Quest not found")
    if any(event_type not in ALLOWED_EVENT_POINTS for event_type in body.event_types):
        raise HTTPException(422, "Policy contains an unsupported event type")
    rules = json.loads(quest.rules_json or "{}")
    rules["verified_event_auto_progress"] = {"enabled": body.enabled, "event_types": sorted(set(body.event_types))}
    quest.rules_json = json.dumps(rules, sort_keys=True)
    db.commit()
    return {"quest_id": quest.id, "enabled": body.enabled, "event_types": sorted(set(body.event_types)),
            "note": "Only verified, non-simulated Twitch EventSub records can trigger this policy. The server uses fixed event-to-point rules; AI cannot invent events."}

def award_verified_event_coins(db, workspace_key: str, event: WorkspaceEvent) -> bool:
    if event.simulated or event.source != "twitch": return False
    policy = db.get(EconomyPolicy, workspace_key)
    if not policy or not policy.enabled: return False
    try: rewards = json.loads(policy.rules_json or "{}")
    except ValueError: rewards = {}
    amount = int(rewards.get(event.event_type, 0) or 0)
    if amount <= 0: return False
    try: payload = json.loads(event.payload_json or "{}")
    except ValueError: payload = {}
    viewer_id = str(payload.get("user_id") or payload.get("from_broadcaster_user_id") or "")
    display_name = str(payload.get("user_name") or payload.get("from_broadcaster_user_name") or "Viewer")
    if not viewer_id: return False
    if db.query(Ledger).filter_by(workspace_key=workspace_key, viewer_id=viewer_id, reference=event.id).first(): return False
    wallet = _wallet(db, workspace_key, viewer_id, display_name)
    wallet.balance += amount; wallet.updated_at = utcnow()
    _ledger(db, workspace_key, viewer_id, amount, f"Verified {event.event_type} reward", event.id)
    db.commit()
    return True

def apply_verified_event_progress(db, workspace_key: str, event: WorkspaceEvent) -> bool:
    """Optional auto-award path: verified Twitch evidence + creator-configured fixed rules."""
    if event.simulated or event.source != "twitch": return False
    award_verified_event_coins(db, workspace_key, event)
    points = ALLOWED_EVENT_POINTS.get(event.event_type)
    if not points: return False
    quest = db.query(Quest).filter_by(workspace_key=workspace_key, status="active").first()
    if not quest: return False
    try: policy = json.loads(quest.rules_json or "{}").get("verified_event_auto_progress", {})
    except ValueError: policy = {}
    if not policy.get("enabled") or event.event_type not in policy.get("event_types", []): return False
    if db.query(ProgressProposal).filter_by(workspace_key=workspace_key, event_id=event.id).first(): return False
    proposal = ProgressProposal(id=secrets.token_hex(16), workspace_key=workspace_key, quest_id=quest.id,
        event_id=event.id, points=points, reason=f"Automatic fixed-rule credit for verified {event.event_type}", status="auto_approved")
    db.add(proposal); db.flush()
    for index in range(points):
        if quest.status != "active": break
        _advance_quest(db, quest, workspace_key, quest.progress, False, f"auto-progress:{proposal.id}:{index}")
    db.commit()
    return True

@router.post("/expansion/ai-progress/propose")
def ai_progress_propose(body: ProposalIn, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    q=db.query(Quest).filter_by(id=body.quest_id,workspace_key=key,status="active").first()
    e=db.query(WorkspaceEvent).filter_by(id=body.event_id,workspace_key=key).first()
    if not q or not e: raise HTTPException(404,"Active quest or recorded event not found")
    points=ALLOWED_EVENT_POINTS.get(e.event_type)
    if points is None: raise HTTPException(422,"This event type is not eligible for progress credit")
    if db.query(ProgressProposal).filter_by(workspace_key=key,event_id=e.id).first(): raise HTTPException(409,"This event already has a progress proposal")
    p=ProgressProposal(id=secrets.token_hex(16),workspace_key=key,quest_id=q.id,event_id=e.id,points=points,reason=f"Rule-based credit for verified {e.event_type}")
    db.add(p); db.commit(); return {"proposal_id":p.id,"points":points,"status":"pending_creator_approval","evidence":{"event_id":e.id,"event_type":e.event_type,"source":e.source,"simulated":e.simulated},"ai_role":"optional explanation only; points are determined by a fixed server-side rule"}

@router.post("/expansion/ai-progress/{proposal_id}/approve")
def ai_progress_approve(proposal_id: str, request: Request, db=Depends(get_db)):
    key, _ = _workspace(request, db)
    p=db.query(ProgressProposal).filter_by(id=proposal_id,workspace_key=key,status="pending").first()
    if not p: raise HTTPException(404,"Pending progress proposal not found")
    q=db.query(Quest).filter_by(id=p.quest_id,workspace_key=key,status="active").first()
    e=db.query(WorkspaceEvent).filter_by(id=p.event_id,workspace_key=key).first()
    if not q or not e or ALLOWED_EVENT_POINTS.get(e.event_type)!=p.points: raise HTTPException(409,"Recorded evidence or rule changed; proposal cannot be applied")
    for index in range(p.points):
        if q.status != "active": break
        _advance_quest(db,q,key,q.progress,e.simulated,f"ai-progress:{p.id}:{index}")
    p.status="approved"; db.commit()
    return {"proposal_id":p.id,"status":p.status,"progress":q.progress,"quest_id":q.id,"approved_by":"creator"}

@router.get("/expansion/extension/config")
def extension_config():
    return {"extension_type":"panel","status":"developer_console_setup_required","required_setup":["Register a Twitch Extension in the Developer Console","Configure extension client ID and allowed origins","Implement Twitch Extension JWT verification and signed configuration","Submit the extension for Twitch review"],"obs_overlay_remains_available":True,"official_extension_live":False}


def _extension_claims(authorization: str | None):
    if not config.TWITCH_EXTENSION_SECRET_B64:
        raise HTTPException(503, "Twitch Extension shared secret is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Twitch Extension JWT is required")
    try:
        secret = base64.b64decode(config.TWITCH_EXTENSION_SECRET_B64, validate=True)
        claims = jwt.decode(authorization[7:], secret, algorithms=["HS256"], options={"require": ["exp", "channel_id", "role", "opaque_user_id"]})
    except (ValueError, jwt.InvalidTokenError):
        raise HTTPException(401, "Invalid or expired Twitch Extension JWT")
    if claims.get("role") not in {"viewer", "moderator", "broadcaster"}:
        raise HTTPException(403, "Unsupported Twitch Extension role")
    return claims

def _extension_context(authorization: str | None, db):
    claims = _extension_claims(authorization)
    user = db.query(User).filter_by(twitch_id=str(claims["channel_id"])).first()
    if not user: raise HTTPException(404, "This channel has not connected QuestCast yet")
    viewer_id = str(claims.get("user_id") or claims["opaque_user_id"])
    return claims, user, f"twitch:{user.id}", viewer_id

@router.get("/expansion/extension/state")
def extension_state(authorization: str | None = Header(default=None, alias="Authorization"), db=Depends(get_db)):
    claims, user, key, viewer_id = _extension_context(authorization, db)
    quest = db.query(Quest).filter_by(workspace_key=key, status="active").first()
    nodes = db.query(StoryNode).filter_by(workspace_key=key, status="voting").order_by(StoryNode.created_at.desc()).limit(3).all()
    stories=[]
    for node in nodes:
        tally={c["key"]:0 for c in json.loads(node.choices_json)}
        for vote in db.query(StoryVote).filter_by(node_id=node.id).all(): tally[vote.choice_key]=tally.get(vote.choice_key,0)+1
        stories.append({"id":node.id,"title":node.title,"prompt":node.prompt,"choices":json.loads(node.choices_json),"tally":tally})
    return {"channel_id":str(claims["channel_id"]),"role":claims["role"],"quest":_dict_quest(quest,db) if quest else None,"stories":stories}

@router.get("/expansion/extension/wallet")
def extension_wallet(authorization: str | None = Header(default=None, alias="Authorization"), db=Depends(get_db)):
    claims, user, key, viewer_id = _extension_context(authorization, db)
    wallet = db.query(Wallet).filter_by(workspace_key=key, viewer_id=viewer_id).first()
    inventory = db.query(Inventory).filter_by(workspace_key=key, viewer_id=viewer_id).all()
    return {"viewer_id":viewer_id,"balance":wallet.balance if wallet else 0,
      "inventory":[{"item_key":x.item_key,"name":CATALOG.get(x.item_key,{}).get("name",x.item_key),"quantity":x.quantity} for x in inventory],
      "catalog":CATALOG}

@router.post("/expansion/extension/economy/purchase")
def extension_purchase(body: dict, authorization: str | None = Header(default=None, alias="Authorization"), db=Depends(get_db)):
    claims, user, key, viewer_id = _extension_context(authorization, db)
    if claims.get("role") not in {"viewer","moderator","broadcaster"}: raise HTTPException(403,"This role cannot purchase abilities")
    item_key = str(body.get("item_key") or "")
    item = CATALOG.get(item_key)
    if not item: raise HTTPException(404,"Unknown item")
    wallet = _wallet(db,key,viewer_id,str(claims.get("user_id") or "Viewer"))
    if wallet.balance < item["cost"]: raise HTTPException(409,"Not enough Quest Coins")
    wallet.balance -= item["cost"]
    inv=db.query(Inventory).filter_by(workspace_key=key,viewer_id=viewer_id,item_key=item_key).first()
    if not inv: inv=Inventory(workspace_key=key,viewer_id=viewer_id,item_key=item_key,quantity=0); db.add(inv)
    inv.quantity += 1; _ledger(db,key,viewer_id,-item["cost"],f"Purchased {item['name']}")
    db.commit(); return {"balance":wallet.balance,"item":item_key,"quantity":inv.quantity}

@router.post("/expansion/extension/economy/trades")
def extension_trade_create(body: dict, authorization: str | None = Header(default=None, alias="Authorization"), db=Depends(get_db)):
    claims, user, key, viewer_id = _extension_context(authorization, db)
    if viewer_id.startswith("A"): raise HTTPException(403,"Sign in to Twitch and enable identity linking to use persistent trading")
    to_viewer=str(body.get("to_viewer") or "")
    item_key=str(body.get("item_key") or "")
    quantity=int(body.get("quantity") or 0)
    if not to_viewer or to_viewer==viewer_id or item_key not in CATALOG or quantity<1 or quantity>20:
        raise HTTPException(422,"Invalid trade request")
    inv=db.query(Inventory).filter_by(workspace_key=key,viewer_id=viewer_id,item_key=item_key).first()
    if not inv or inv.quantity<quantity: raise HTTPException(409,"Insufficient inventory")
    trade=Trade(id=secrets.token_hex(16),workspace_key=key,from_viewer=viewer_id,to_viewer=to_viewer,item_key=item_key,quantity=quantity)
    db.add(trade); db.commit(); return {"trade_id":trade.id,"status":trade.status}

@router.post("/expansion/extension/economy/trades/{trade_id}/accept")
def extension_trade_accept(trade_id: str, authorization: str | None = Header(default=None, alias="Authorization"), db=Depends(get_db)):
    claims, user, key, viewer_id = _extension_context(authorization, db)
    trade=db.query(Trade).filter_by(id=trade_id,workspace_key=key,to_viewer=viewer_id,status="pending").first()
    if not trade: raise HTTPException(404,"Trade not found for this viewer")
    inv=db.query(Inventory).filter_by(workspace_key=key,viewer_id=trade.from_viewer,item_key=trade.item_key).first()
    if not inv or inv.quantity<trade.quantity: raise HTTPException(409,"Sender no longer owns the offered quantity")
    inv.quantity-=trade.quantity
    target=db.query(Inventory).filter_by(workspace_key=key,viewer_id=viewer_id,item_key=trade.item_key).first()
    if not target: target=Inventory(workspace_key=key,viewer_id=viewer_id,item_key=trade.item_key,quantity=0); db.add(target)
    target.quantity+=trade.quantity; trade.status="accepted"; db.commit()
    return {"trade_id":trade.id,"status":trade.status}

@router.post("/expansion/extension/economy/use")
def extension_use(body: dict, authorization: str | None = Header(default=None, alias="Authorization"), db=Depends(get_db)):
    claims, user, key, viewer_id = _extension_context(authorization, db)
    item_key=str(body.get("item_key") or "")
    inv=db.query(Inventory).filter_by(workspace_key=key,viewer_id=viewer_id,item_key=item_key).first()
    if not inv or inv.quantity < 1: raise HTTPException(409,"You do not own this ability")
    if item_key == "reroll":
        job=IntegrationJob(id=secrets.token_hex(16),workspace_key=key,kind="ability",status="awaiting_creator_review",payload_json=json.dumps({"ability":item_key,"viewer_id":viewer_id}),detail="Viewer requested a story-vote reroll; creator must approve")
        db.add(job); effect="Reroll request sent to creator review"
    elif item_key in {"spotlight","confetti"}:
        from backend.workspace import _record_event
        _record_event(db,key,"economy.ability_used",{"display_name":str(claims.get("user_id") or "Viewer"),"ability":item_key},"twitch_extension",f"ext-ability:{secrets.token_hex(12)}",False)
        job=IntegrationJob(id=secrets.token_hex(16),workspace_key=key,kind="ability",status="triggered",payload_json=json.dumps({"ability":item_key,"viewer_id":viewer_id}),detail="Extension ability event recorded")
        db.add(job); effect="Ability event recorded"
    else: raise HTTPException(422,"Unknown ability")
    inv.quantity-=1; db.commit(); return {"effect":effect,"remaining":inv.quantity}

@router.post("/expansion/extension/story/{node_id}/vote")
def extension_vote(node_id: str, body: dict, authorization: str | None = Header(default=None, alias="Authorization"), db=Depends(get_db)):
    claims, owner, key, viewer_id = _extension_context(authorization, db)
    if claims.get("role") not in {"viewer", "moderator", "broadcaster"}:
        raise HTTPException(403, "This role cannot vote")
    node = db.query(StoryNode).filter_by(id=node_id,workspace_key=key,status="voting").first()
    if not node: raise HTTPException(404, "Open story vote not found")
    choice = str(body.get("choice_key") or "")
    if choice not in {c["key"] for c in json.loads(node.choices_json)}: raise HTTPException(422, "Invalid story choice")
    existing=db.query(StoryVote).filter_by(node_id=node.id,viewer_id=viewer_id).first()
    if existing: existing.choice_key=choice
    else: db.add(StoryVote(node_id=node.id,viewer_id=viewer_id,choice_key=choice))
    db.commit(); return {"accepted":True,"choice_key":choice}
