import os
import tempfile

from cryptography.fernet import Fernet

# Must run before any backend import: gives the tests an isolated DB and "live-mode" config.
_db = os.path.join(tempfile.gettempdir(), "questcast_test.db")
if os.path.exists(_db):
    os.remove(_db)
os.environ.update(DATABASE_URL=f"sqlite:///{_db}", TWITCH_CLIENT_ID="test-id", TWITCH_CLIENT_SECRET="test-secret",
                  TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(), TWITCH_EVENTSUB_SECRET="webhook-test-secret")

from backend.db import Base, engine  # noqa: E402

Base.metadata.create_all(engine)
