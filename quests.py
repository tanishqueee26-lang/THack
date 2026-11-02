"""Quest progression derived ONLY from stored real Twitch events (pure logic, no I/O)."""
from datetime import timedelta

from backend.window import dedupe

WINDOW = timedelta(days=7)
QUESTS = [
    {"id": "follower-rush", "name": "Follower Rush", "desc": "Gain 10 followers this week", "icon": "🚂", "events": ("channel.follow",), "target": 10},
    {"id": "sub-surge", "name": "Sub Surge", "desc": "Get 3 subscriptions this week", "icon": "💎", "events": ("channel.subscribe",), "target": 3},
    {"id": "raid-party", "name": "Raid Party", "desc": "Receive a raid this week", "icon": "⚔️", "events": ("channel.raid",), "target": 1},
    {"id": "chatterbox", "name": "Chatterbox", "desc": "Reach 100 chat messages this week", "icon": "💬", "events": ("channel.chat.message",), "target": 100},
]


def progress(events, now):
    """events: iterable of {"type": str, "at": naive-UTC datetime}. Returns one progress row per quest."""
    events = dedupe(list(events))  # a repeated delivery (same id) must not count twice
    out = []
    for q in QUESTS:
        n = sum(1 for e in events if e["type"] in q["events"] and now - WINDOW <= e["at"] <= now)
        out.append({"id": q["id"], "name": q["name"], "desc": q["desc"], "icon": q["icon"], "target": q["target"],
                    "count": n, "completed": n >= q["target"], "pct": min(100, round(100 * n / q["target"]))})
    return out
