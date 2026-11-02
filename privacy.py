"""Privacy-minimizing persistence helpers for Twitch event payloads."""
from __future__ import annotations
import hashlib


def sanitize_twitch_event(event_type: str, payload: dict) -> dict:
    """Remove raw chat content before storing Event/WorkspaceEvent records."""
    if event_type == "channel.chat.message":
        message_text = str((payload.get("message") or {}).get("text") or "")
        normalized = " ".join(message_text.casefold().split())
        return {
            "broadcaster_user_id": str(payload.get("broadcaster_user_id") or ""),
            "chatter_user_id": str(payload.get("chatter_user_id") or ""),
            "chatter_user_login": str(payload.get("chatter_user_login") or "")[:64],
            "chatter_user_name": str(payload.get("chatter_user_name") or "")[:64],
            "message_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else "",
            "message_length": min(len(message_text), 500),
        }
    if event_type in {"channel.suspicious_user.message", "channel.suspicious_user.update"}:
        return {k: payload.get(k) for k in (
            "broadcaster_user_id", "user_id", "user_login", "user_name", "low_trust_status",
            "types", "ban_evasion_evaluation", "shared_ban_channel_ids",
        ) if k in payload}
    return payload
