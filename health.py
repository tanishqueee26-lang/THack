"""In-memory integration health. Reflects only what actually happened – nothing is pre-filled."""
from collections import deque
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserHealth:
    def __init__(self):
        self.eventsub = {"status": "disconnected", "session_id": None, "connected_at": None,
                         "last_message_at": None, "reconnects": 0, "subscriptions": []}
        self.chat = {"status": "disconnected", "detail": "", "last_message_at": None, "messages_seen": 0}
        self.token = {"valid": None, "checked_at": None, "expires_in": None}  # None = not checked yet
        self.last_event = None
        self.errors = deque(maxlen=25)


_users: dict[int, UserHealth] = {}
sim_events: deque = deque(maxlen=25)  # simulation only; never mixed with real events


def get(user_id: int) -> UserHealth:
    return _users.setdefault(user_id, UserHealth())


def log(user_id: int, kind: str, status: str, detail: str = "") -> None:
    """Persist a history row. Never allowed to break the integration itself."""
    try:
        from backend.db import HealthEvent, SessionLocal
        with SessionLocal() as db:
            db.add(HealthEvent(user_id=user_id, kind=kind, status=status[:64], detail=detail[:300]))
            db.commit()
    except Exception:
        pass


def history(user_id: int, limit: int = 15) -> list[dict]:
    from backend.db import HealthEvent, SessionLocal
    with SessionLocal() as db:
        rows = db.query(HealthEvent).filter_by(user_id=user_id).order_by(HealthEvent.id.desc()).limit(limit)
        return [{"at": r.created_at.isoformat() + "Z", "kind": r.kind, "status": r.status, "detail": r.detail} for r in rows]


def set_component(user_id: int, name: str, **fields) -> None:
    """Update eventsub/chat state; persist a history row only when the status actually changes."""
    comp = getattr(get(user_id), name)
    old = comp.get("status")
    comp.update(fields)
    if "status" in fields and fields["status"] != old:
        log(user_id, name, fields["status"], comp.get("detail", "") if name == "chat" else "")


def record_error(user_id: int | None, source: str, message: str) -> None:
    if user_id is not None:
        get(user_id).errors.appendleft({"at": now(), "source": source, "message": message[:300]})
        log(user_id, "error", source, message)
