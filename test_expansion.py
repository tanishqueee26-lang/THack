import pytest
from fastapi.testclient import TestClient
from backend import config
from backend.db import Base, engine, SessionLocal, utcnow
from backend.main import app
from backend.expansion import Wallet, Ledger, Inventory, Trade, StoryNode, StoryVote, IntegrationJob, ProgressProposal, EconomyPolicy
from backend.workspace import Quest, WorkspaceEvent, Participation, Candidate, MemoryEntry, ModeratorGrant, ChatCommandCooldown, ClipCooldown, BotReview

Base.metadata.create_all(engine)
client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_expansion(monkeypatch):
    monkeypatch.setattr(config, "APP_MODE", "simulation")
    with SessionLocal() as db:
        for model in [StoryVote, Trade, ProgressProposal, IntegrationJob, Inventory, Ledger, EconomyPolicy, Wallet, StoryNode,
                      Candidate, Participation, WorkspaceEvent, MemoryEntry, Quest, ModeratorGrant,
                      ChatCommandCooldown, ClipCooldown, BotReview]:
            db.query(model).delete(synchronize_session=False)
        db.commit()


def test_economy_award_purchase_and_transfer():
    r=client.post('/api/workspace/expansion/economy/award',json={'viewer_id':'alice','amount':100,'reason':'demo reward'})
    assert r.status_code==200 and r.json()['balance']==100
    r=client.post('/api/workspace/expansion/economy/purchase',json={'viewer_id':'alice','item_key':'confetti'})
    assert r.status_code==200 and r.json()['balance']==75
    t=client.post('/api/workspace/expansion/economy/trades',json={'from_viewer':'alice','to_viewer':'bob','item_key':'confetti','quantity':1})
    assert t.status_code==200
    accepted=client.post(f"/api/workspace/expansion/economy/trades/{t.json()['trade_id']}/accept")
    assert accepted.status_code==200
    assert client.get('/api/workspace/expansion/economy/bob').json()['inventory'][0]['quantity']==1


def test_branching_story_vote_and_lock():
    n=client.post('/api/workspace/expansion/story/nodes',json={'story_key':'ch1','title':'Choose a path','prompt':'Which way?','choices':[{'key':'A','label':'Forest'},{'key':'B','label':'Mountain'}]})
    assert n.status_code==200
    nid=n.json()['id']
    assert client.post(f'/api/workspace/expansion/story/{nid}/vote',json={'viewer_id':'v1','choice_key':'A'}).status_code==200
    assert client.post(f'/api/workspace/expansion/story/{nid}/vote',json={'viewer_id':'v2','choice_key':'A'}).status_code==200
    locked=client.post(f'/api/workspace/expansion/story/{nid}/lock')
    assert locked.status_code==200 and locked.json()['selected_choice']=='A'


def test_branch_tie_requires_creator_resolution():
    n=client.post('/api/workspace/expansion/story/nodes',json={'story_key':'tie','title':'Tie','choices':[{'key':'A','label':'A'},{'key':'B','label':'B'}]}).json()
    for v,k in [('v1','A'),('v2','B')]:
        client.post(f"/api/workspace/expansion/story/{n['id']}/vote",json={'viewer_id':v,'choice_key':k})
    assert client.post(f"/api/workspace/expansion/story/{n['id']}/lock").status_code==409


def test_vision_and_social_jobs_never_claim_live_success():
    vision=client.post('/api/workspace/expansion/vision/jobs',json={'vod_url':'https://example.com/vod.mp4','consent_confirmed':True})
    assert vision.status_code==200 and vision.json()['status']=='blocked_unconfigured'
    assert 'No video was fetched' in vision.json()['detail']
    publish=client.post('/api/workspace/expansion/publish',json={'candidate_id':'missing','platform':'youtube','caption':'x'})
    assert publish.status_code==409


def test_gameplay_sandbox_is_not_real_control():
    r=client.post('/api/workspace/expansion/gameplay/propose',json={'adapter':'sandbox','action':'show_hint','payload':{'hint':'hello'}})
    assert r.status_code==200 and r.json()['status']=='simulated'
    assert 'no gameplay process was controlled' in r.json()['detail']


def test_progress_proposal_requires_recorded_eligible_event_and_creator_approval():
    q=client.post('/api/workspace/quests',json={'title':'Evidence quest','target':10,'stages':['A','B']}).json()
    client.post(f"/api/workspace/quests/{q['id']}/activate")
    ev=client.post('/api/workspace/demo/events',json={'type':'channel.follow','display_name':'Follower'}).json()['event_ids'][0]
    p=client.post('/api/workspace/expansion/ai-progress/propose',json={'quest_id':q['id'],'event_id':ev})
    assert p.status_code==200 and p.json()['points']==1
    approved=client.post(f"/api/workspace/expansion/ai-progress/{p.json()['proposal_id']}/approve")
    assert approved.status_code==200 and approved.json()['progress']==1
    assert client.post(f"/api/workspace/expansion/ai-progress/{p.json()['proposal_id']}/approve").status_code==404


def test_assistant_state_lists_only_workspace_evidence_and_pending_reviews():
    q=client.post('/api/workspace/quests',json={'title':'Assistant quest','target':10,'stages':['A','B']}).json()
    client.post(f"/api/workspace/quests/{q['id']}/activate")
    event_id=client.post('/api/workspace/demo/events',json={'type':'channel.follow','display_name':'Evidence viewer'}).json()['event_ids'][0]
    proposal=client.post('/api/workspace/expansion/ai-progress/propose',json={'quest_id':q['id'],'event_id':event_id}).json()
    state=client.get('/api/workspace/expansion/assistant-state')
    assert state.status_code==200
    body=state.json()
    assert body['quests'][0]['id']==q['id']
    assert any(event['id']==event_id and event['points']==1 and event['simulated'] for event in body['events'])
    assert body['proposals'][0]['id']==proposal['proposal_id']
    assert body['proposals'][0]['event_id']==event_id


def test_extension_status_is_truthful():
    r=client.get('/api/workspace/expansion/extension/config')
    assert r.status_code==200 and r.json()['official_extension_live'] is False


def test_verified_event_auto_progress_requires_creator_policy(monkeypatch):
    from backend.workspace import ingest_twitch_event
    q=client.post('/api/workspace/quests',json={'title':'Auto quest','target':8,'stages':['A','B']}).json()
    client.post(f"/api/workspace/quests/{q['id']}/activate")
    policy=client.post('/api/workspace/expansion/ai-progress/policy',json={'quest_id':q['id'],'enabled':True,'event_types':['channel.follow']})
    assert policy.status_code==200
    ingest_twitch_event('demo','channel.follow',{'user_name':'RealSignal'},'auto-event-1')
    result=client.get('/api/workspace/quests').json()
    assert next(x for x in result if x['id']==q['id'])['progress']==1
    # The same event ID is idempotent and cannot award twice.
    ingest_twitch_event('demo','channel.follow',{'user_name':'RealSignal'},'auto-event-1')
    result=client.get('/api/workspace/quests').json()
    assert next(x for x in result if x['id']==q['id'])['progress']==1


def test_twitch_extension_jwt_and_story_vote(monkeypatch):
    import base64, jwt, time
    from backend.db import User
    secret=base64.b64encode(b'questcast-extension-test-secret-32-byte-key').decode()
    monkeypatch.setattr(config,'TWITCH_EXTENSION_SECRET_B64',secret)
    with SessionLocal() as db:
        owner=User(twitch_id='channel-12345',login='creator',display_name='Creator')
        db.add(owner); db.commit(); db.refresh(owner)
        key=f'twitch:{owner.id}'
        db.add(Wallet(workspace_key=key,viewer_id='U-viewer-1',balance=100))
        node=StoryNode(id='extension-story-1',workspace_key=key,story_key='ext1',title='Extension vote',prompt='Choose',choices_json='[{"key":"A","label":"A"},{"key":"B","label":"B"}]',status='voting')
        db.add(node); db.commit()
    token=jwt.encode({'exp':int(time.time())+300,'channel_id':'channel-12345','role':'viewer','opaque_user_id':'U-viewer-1'},base64.b64decode(secret),algorithm='HS256')
    headers={'Authorization':f'Bearer {token}'}
    state=client.get('/api/workspace/expansion/extension/state',headers=headers)
    assert state.status_code==200 and state.json()['stories'][0]['title']=='Extension vote'
    wallet=client.get('/api/workspace/expansion/extension/wallet',headers=headers)
    assert wallet.status_code==200 and wallet.json()['balance']==100
    bought=client.post('/api/workspace/expansion/extension/economy/purchase',headers=headers,json={'item_key':'confetti'})
    assert bought.status_code==200 and bought.json()['balance']==75
    used=client.post('/api/workspace/expansion/extension/economy/use',headers=headers,json={'item_key':'confetti'})
    assert used.status_code==200 and used.json()['remaining']==0
    client.post('/api/workspace/expansion/extension/economy/purchase',headers=headers,json={'item_key':'spotlight'})
    trade=client.post('/api/workspace/expansion/extension/economy/trades',headers=headers,json={'to_viewer':'U-viewer-2','item_key':'spotlight','quantity':1})
    assert trade.status_code==200
    recipient=jwt.encode({'exp':int(time.time())+300,'channel_id':'channel-12345','role':'viewer','opaque_user_id':'U-viewer-2'},base64.b64decode(secret),algorithm='HS256')
    accepted=client.post(f"/api/workspace/expansion/extension/economy/trades/{trade.json()['trade_id']}/accept",headers={'Authorization':f'Bearer {recipient}'})
    assert accepted.status_code==200 and accepted.json()['status']=='accepted'
    vote=client.post('/api/workspace/expansion/extension/story/extension-story-1/vote',headers=headers,json={'choice_key':'B'})
    assert vote.status_code==200 and vote.json()['accepted'] is True
    assert client.get('/api/workspace/expansion/extension/state',headers={'Authorization':'Bearer invalid'}).status_code==401


def test_branch_child_must_follow_locked_parent_choice():
    parent=client.post('/api/workspace/expansion/story/nodes',json={'story_key':'parent','title':'Parent','choices':[{'key':'A','label':'A'},{'key':'B','label':'B'}]}).json()
    for v in ['v1','v2']:
        client.post(f"/api/workspace/expansion/story/{parent['id']}/vote",json={'viewer_id':v,'choice_key':'A'})
    client.post(f"/api/workspace/expansion/story/{parent['id']}/lock")
    wrong=client.post('/api/workspace/expansion/story/nodes',json={'story_key':'wrong-child','title':'Wrong','parent_node_id':parent['id'],'parent_choice_key':'B','choices':[{'key':'A','label':'A'},{'key':'B','label':'B'}]})
    assert wrong.status_code==409
    right=client.post('/api/workspace/expansion/story/nodes',json={'story_key':'right-child','title':'Right','parent_node_id':parent['id'],'parent_choice_key':'A','choices':[{'key':'A','label':'A'},{'key':'B','label':'B'}]})
    assert right.status_code==200 and right.json()['parent_node_id']==parent['id']


def test_viewer_ability_consumes_inventory_and_reroll_requires_creator_review():
    client.post('/api/workspace/expansion/economy/award',json={'viewer_id':'viewer-ability','amount':100,'reason':'test'})
    client.post('/api/workspace/expansion/economy/purchase',json={'viewer_id':'viewer-ability','item_key':'reroll'})
    used=client.post('/api/workspace/expansion/economy/use',json={'viewer_id':'viewer-ability','item_key':'reroll'})
    assert used.status_code==200 and used.json()['remaining']==0
    with SessionLocal() as db:
        job=db.query(IntegrationJob).filter_by(kind='ability').first()
        assert job.status=='awaiting_creator_review'


def test_verified_twitch_events_can_earn_configured_coins_idempotently():
    from backend.workspace import ingest_twitch_event
    policy=client.post('/api/workspace/expansion/economy/policy',json={'enabled':True,'event_rewards':{'channel.follow':5,'channel.subscribe':20}})
    assert policy.status_code==200
    ingest_twitch_event('demo','channel.follow',{'user_id':'twitch-viewer-5','user_name':'Follower'},'coin-event-5')
    ingest_twitch_event('demo','channel.follow',{'user_id':'twitch-viewer-5','user_name':'Follower'},'coin-event-5')
    wallet=client.get('/api/workspace/expansion/economy/twitch-viewer-5')
    assert wallet.status_code==200 and wallet.json()['balance']==5 and wallet.json()['level']==1


def test_publish_request_requires_approved_verified_clip_and_stays_queued():
    from backend.workspace import Candidate
    import secrets
    with SessionLocal() as db:
        c=Candidate(id=secrets.token_hex(16),workspace_key='demo',fingerprint=secrets.token_hex(32),source='milestone',
            start_at=utcnow(),end_at=utcnow(),score=5,confidence=.8,reasons_json='{}',status='approved',clip_id='clip-verified',clip_url='https://clips.twitch.tv/verified',clip_status='verified')
        db.add(c); db.commit(); cid=c.id
    r=client.post('/api/workspace/expansion/publish',json={'candidate_id':cid,'platform':'youtube','caption':'Approved clip'})
    assert r.status_code==200 and r.json()['published'] is False and r.json()['status']=='awaiting_platform_oauth'
