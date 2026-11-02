import asyncio
import hashlib
import hmac
import time
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from backend import config, health
from backend.db import User, utcnow

ID = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
_chat_send_locks: dict[str, asyncio.Lock] = {}
_chat_last_sent: dict[str, float] = {}


def authorize_url(state: str) -> str:
    q = urlencode({"client_id": config.CLIENT_ID, "redirect_uri": config.REDIRECT_URI,
                   "response_type": "code", "scope": config.SCOPES, "state": state})
    return f"{ID}/authorize?{q}"


async def _token_request(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{ID}/token", data={"client_id": config.CLIENT_ID,
                                              "client_secret": config.CLIENT_SECRET, **data})
        r.raise_for_status()
        return r.json()


async def exchange_code(code: str) -> dict:
    return await _token_request({"code": code, "grant_type": "authorization_code",
                                 "redirect_uri": config.REDIRECT_URI})


class TwitchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


def _apply(user: User, tok: dict) -> None:
    user.access_token = tok["access_token"]
    user.refresh_token = tok.get("refresh_token", user.refresh_token)
    user.token_expires_at = utcnow() + timedelta(seconds=tok.get("expires_in", 3600))
    if tok.get("scope"):
        user.scopes = " ".join(tok["scope"])


async def refresh(user: User, db) -> None:
    h = health.get(user.id)
    try:
        if not user.refresh_token:
            raise RuntimeError("no refresh token")
        _apply(user, await _token_request({"grant_type": "refresh_token", "refresh_token": user.refresh_token}))
    except Exception as e:
        if h.token["valid"] is not False:
            health.log(user.id, "token", "invalid", str(e))
        h.token["valid"] = False
        health.record_error(user.id, "token refresh", f"{e}. Reconnect Twitch.")
        raise
    db.add(user)
    db.commit()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Client-Id": config.CLIENT_ID}


async def get_me(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{HELIX}/users", headers=_headers(access_token))
        r.raise_for_status()
        return r.json()["data"][0]


async def validate(access_token: str) -> dict:
    """GET /oauth2/validate – authoritative granted scopes; Twitch asks apps to call it hourly."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{ID}/validate", headers={"Authorization": f"OAuth {access_token}"})
        r.raise_for_status()
        return r.json()


async def revoke(access_token: str) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{ID}/revoke", data={"client_id": config.CLIENT_ID, "token": access_token})


async def validate_and_store(user: User, db) -> bool:
    h = health.get(user.id)
    try:
        try:
            v = await validate(user.access_token)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 401:
                raise
            await refresh(user, db)  # marks token invalid + records error if this fails
            v = await validate(user.access_token)
    except httpx.HTTPError as e:
        health.record_error(user.id, "token validate", f"{type(e).__name__}: {e}")
        return False
    except Exception:
        return False
    user.scopes = " ".join(v.get("scopes", []))
    db.commit()
    if h.token["valid"] is not True:
        health.log(user.id, "token", "valid")
    h.token.update(valid=True, checked_at=health.now(), expires_in=v.get("expires_in"))
    return True


async def helix(user: User, db, path: str, params: dict | None = None, method: str = "GET",
                json: dict | None = None) -> dict:
    if user.token_expires_at and user.token_expires_at - utcnow() < timedelta(seconds=60):
        await refresh(user, db)

    async def go():
        async with httpx.AsyncClient(timeout=10) as c:
            return await c.request(method, HELIX + path, params=params, json=json,
                                   headers=_headers(user.access_token))

    r = await go()
    if r.status_code == 401:
        await refresh(user, db)
        r = await go()
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except Exception:
            msg = r.text
        health.record_error(user.id, f"{method} {path}", f"{r.status_code}: {msg}")
        raise TwitchError(r.status_code, msg)
    return r.json() if r.content else {}


# (event type, version, scope required or None, condition builder) – per Twitch EventSub reference
SUBSCRIPTIONS = [
    ("stream.online", "1", None, lambda b: {"broadcaster_user_id": b}),
    ("stream.offline", "1", None, lambda b: {"broadcaster_user_id": b}),
    ("channel.raid", "1", None, lambda b: {"to_broadcaster_user_id": b}),
    ("channel.follow", "2", "moderator:read:followers", lambda b: {"broadcaster_user_id": b, "moderator_user_id": b}),
    ("channel.subscribe", "1", "channel:read:subscriptions", lambda b: {"broadcaster_user_id": b}),
    ("channel.chat.message", "1", "user:read:chat", lambda b: {"broadcaster_user_id": b, "user_id": b}),
    ("channel.suspicious_user.message", "1", "moderator:read:suspicious_users", lambda b: {"broadcaster_user_id": b, "moderator_user_id": b}),
    ("channel.suspicious_user.update", "1", "moderator:read:suspicious_users", lambda b: {"broadcaster_user_id": b, "moderator_user_id": b}),
]


async def create_ws_subscription(user: User, db, typ: str, ver: str, cond: dict, session_id: str) -> str:
    r = await helix(user, db, "/eventsub/subscriptions", method="POST", json={
        "type": typ, "version": ver, "condition": cond,
        "transport": {"method": "websocket", "session_id": session_id}})
    return r["data"][0]["status"]  # "enabled" when Twitch accepted it


async def send_chat(user: User, db, message: str) -> dict:
    """Send Chat Message (needs user:write:chat); serialize sends to at most one per second."""
    key = str(user.twitch_id)
    lock = _chat_send_locks.setdefault(key, asyncio.Lock())
    async with lock:
        wait = 1.0 - (time.monotonic() - _chat_last_sent.get(key, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _chat_last_sent[key] = time.monotonic()
        r = await helix(user, db, "/chat/messages", method="POST",
                        json={"broadcaster_id": user.twitch_id, "sender_id": user.twitch_id, "message": message})
        d = r["data"][0]
        return {"sent": bool(d.get("is_sent")), "message_id": d.get("message_id"), "drop_reason": d.get("drop_reason")}


async def create_stream_marker(user: User, db, description: str = "") -> dict:
    if "channel:manage:broadcast" not in user.scopes.split():
        raise TwitchError(403, "Missing channel:manage:broadcast scope")
    result = await helix(user, db, "/streams/markers", method="POST",
                         json={"user_id": user.twitch_id, "description": description[:140]})
    data = result.get("data") or []
    if not data:
        raise TwitchError(502, "Twitch did not return a stream marker")
    return data[0]


def _parse_vod_duration(value: str) -> int:
    """Parse Twitch ISO-8601-like durations such as 1h2m3s into seconds."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", str(value or ""))
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(float(hours or 0) * 3600 + float(minutes or 0) * 60 + float(seconds or 0))


async def create_clip_from_vod(user: User, db, candidate, title: str | None = None) -> dict:
    """Create a timestamp-aligned clip from the matching Twitch archive VOD, then verify it."""
    if "channel:manage:clips" not in user.scopes.split() and "editor:manage:clips" not in user.scopes.split():
        raise TwitchError(403, "Missing channel:manage:clips or editor:manage:clips scope")
    videos = await helix(user, db, "/videos", {"user_id": user.twitch_id, "type": "archive", "sort": "time", "first": 10})
    start_at = candidate.start_at.replace(tzinfo=timezone.utc) if candidate.start_at.tzinfo is None else candidate.start_at.astimezone(timezone.utc)
    end_at = candidate.end_at.replace(tzinfo=timezone.utc) if candidate.end_at.tzinfo is None else candidate.end_at.astimezone(timezone.utc)
    midpoint = start_at + (end_at - start_at) / 2
    selected, selected_start, selected_duration = None, None, 0
    for video in videos.get("data", []):
        try:
            created = datetime.fromisoformat(video["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        duration = _parse_vod_duration(video.get("duration", ""))
        if not duration:
            continue
        video_end = created + timedelta(seconds=duration)
        # A small boundary allowance accounts for Twitch's stream/VOD creation timestamp skew.
        if created - timedelta(seconds=120) <= midpoint <= video_end + timedelta(seconds=120):
            selected, selected_start, selected_duration = video, created, duration
            break
    if not selected:
        raise TwitchError(409, "No matching Twitch archive VOD is available for this candidate yet. Keep the candidate and retry when the VOD appears.")
    duration_s = min(30, selected_duration)
    if duration_s < 5:
        raise TwitchError(409, "The matching VOD is shorter than Twitch's 5-second minimum clip duration.")
    event_offset = int((midpoint - selected_start).total_seconds())
    vod_offset = min(selected_duration, max(duration_s, event_offset + duration_s // 2))
    if vod_offset < duration_s or vod_offset > selected_duration:
        raise TwitchError(409, "The candidate timestamp is outside the available VOD range; no clip was created.")
    clip_title = (title or f"QuestCast highlight · {candidate.source.replace('_', ' ')}").strip()[:140]
    created = await helix(user, db, "/videos/clips", {
        "broadcaster_id": user.twitch_id, "editor_id": user.twitch_id, "vod_id": selected["id"],
        "vod_offset": vod_offset, "duration": duration_s, "title": clip_title,
    }, method="POST")
    clip = (created.get("data") or [{}])[0]
    if not clip.get("id"):
        raise TwitchError(502, "Twitch accepted no clip identifier; the candidate remains unverified.")
    for _ in range(20):
        await asyncio.sleep(3)
        got = await helix(user, db, "/clips", {"id": clip["id"]})
        if got.get("data"):
            return {"status": "verified", "id": clip["id"], "edit_url": clip.get("edit_url", ""),
                    "url": got["data"][0]["url"], "vod_id": selected["id"], "vod_offset": vod_offset,
                    "duration": duration_s, "title": clip_title}
    return {"status": "unverified", "id": clip["id"], "edit_url": clip.get("edit_url", ""),
            "vod_id": selected["id"], "vod_offset": vod_offset,
            "detail": "Twitch did not return this clip from Get Clips within 60 seconds. No public clip URL was stored."}


async def create_clip(user: User, db) -> dict:
    """Create Clip, then confirm via Get Clips. Current Helix reference says to assume failure after 60 seconds."""
    r = await helix(user, db, "/clips", {"broadcaster_id": user.twitch_id}, method="POST")
    clip = r["data"][0]
    for _ in range(20):
        await asyncio.sleep(3)
        got = await helix(user, db, "/clips", {"id": clip["id"]})
        if got["data"]:
            return {"status": "verified", "id": clip["id"], "edit_url": clip["edit_url"], "url": got["data"][0]["url"]}
    return {"status": "unverified", "id": clip["id"], "edit_url": clip["edit_url"],
            "detail": "Get Clips did not return the clip within 60s. Twitch says to assume it failed; retry."}


async def app_token() -> str:
    return (await _token_request({"grant_type": "client_credentials"}))["access_token"]


async def subscribe_all(user: User) -> list[dict]:
    """Register EventSub webhook subscriptions for a broadcaster (needs a public HTTPS URL)."""
    if not config.PUBLIC_BASE_URL.startswith("https://") or not config.EVENTSUB_SECRET:
        return [{"skipped": "Set PUBLIC_BASE_URL (https) and TWITCH_EVENTSUB_SECRET to enable EventSub"}]
    b = user.twitch_id
    subs = [
        ("stream.online", "1", {"broadcaster_user_id": b}),
        ("stream.offline", "1", {"broadcaster_user_id": b}),
        ("channel.follow", "2", {"broadcaster_user_id": b, "moderator_user_id": b}),
        ("channel.subscribe", "1", {"broadcaster_user_id": b}),
        ("channel.raid", "1", {"to_broadcaster_user_id": b}),
        ("channel.suspicious_user.message", "1", {"broadcaster_user_id": b, "moderator_user_id": b}),
        ("channel.suspicious_user.update", "1", {"broadcaster_user_id": b, "moderator_user_id": b}),
    ]
    token, out = await app_token(), []
    async with httpx.AsyncClient(timeout=10) as c:
        for typ, ver, cond in subs:
            r = await c.post(f"{HELIX}/eventsub/subscriptions", headers=_headers(token), json={
                "type": typ, "version": ver, "condition": cond,
                "transport": {"method": "webhook", "callback": f"{config.PUBLIC_BASE_URL}/webhooks/twitch",
                              "secret": config.EVENTSUB_SECRET}})
            out.append({"type": typ, "status": r.status_code})  # 409 = already subscribed
    return out


def verify_signature(secret: str, msg_id: str, timestamp: str, body: bytes, signature: str) -> bool:
    """EventSub: HMAC-SHA256 over id + timestamp + raw body, compared in constant time."""
    expected = "sha256=" + hmac.new(secret.encode(), msg_id.encode() + timestamp.encode() + body,
                                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
