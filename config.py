import os

try:  # python-dotenv ships with uvicorn[standard]
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


APP_MODE = env("APP_MODE", "auto").lower()  # auto | simulation | live
CLIENT_ID = env("TWITCH_CLIENT_ID")
CLIENT_SECRET = env("TWITCH_CLIENT_SECRET")
REDIRECT_URI = env("TWITCH_REDIRECT_URI", "http://localhost:8011/auth/callback")
EVENTSUB_SECRET = env("TWITCH_EVENTSUB_SECRET")
SCOPES = env("TWITCH_SCOPES", "clips:edit channel:read:subscriptions moderator:read:followers user:read:chat user:write:chat moderator:read:suspicious_users moderator:manage:banned_users channel:manage:clips channel:manage:broadcast")
FRONTEND_URL = env("FRONTEND_URL", "http://localhost:3000")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")  # public HTTPS URL of this API (ngrok etc.)
DATABASE_URL = env("DATABASE_URL", "sqlite:///./database/questcast.db")
COOKIE_SECURE = env("COOKIE_SECURE", "false").lower() == "true"
SESSION_DAYS = 7
GOALS = {  # StreamQuest targets
    "followers": int(env("QUEST_FOLLOWER_GOAL", "100")),
    "subscribers": int(env("QUEST_SUB_GOAL", "10")),
    "events": int(env("QUEST_EVENT_GOAL", "50")),
}
ENCRYPTION_KEY = env("TOKEN_ENCRYPTION_KEY")
TRANSPORT = env("EVENT_TRANSPORT", "websocket")  # "websocket" (local dev) or "webhook" (production)
EVENTSUB_WS_URL = env("EVENTSUB_WS_URL", "wss://eventsub.wss.twitch.tv/ws")


def missing_config() -> list[str]:
    """Names of required Twitch settings that are unset or still placeholders."""
    need = {"TWITCH_CLIENT_ID": CLIENT_ID, "TWITCH_CLIENT_SECRET": CLIENT_SECRET, "TOKEN_ENCRYPTION_KEY": ENCRYPTION_KEY}
    return [k for k, v in need.items() if not v or v.startswith(("your_", "change_me"))]


def operating_mode() -> str:
    """Explicit simulation/live mode; auto preserves the legacy credential-based behavior."""
    if APP_MODE in {"simulation", "live"}:
        return APP_MODE
    return "simulation" if missing_config() else "live"


def live_ready() -> bool:
    return operating_mode() == "live" and not missing_config()

AUTO_CREATE_SCHEMA = env("AUTO_CREATE_SCHEMA", "true").lower() == "true"

AI_BASE_URL = env("AI_BASE_URL")
AI_API_KEY = env("AI_API_KEY")
AI_MODEL = env("AI_MODEL")
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4o-mini")
TWITCH_EXTENSION_SECRET_B64 = env("TWITCH_EXTENSION_SECRET_B64")
TWITCH_EXTENSION_ORIGIN = env("TWITCH_EXTENSION_ORIGIN")
