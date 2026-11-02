"""INTEGRATION tests that need real Twitch credentials, network access, or real captured Twitch data.

They are SKIPPED (never passed) unless you opt in:
    RUN_TWITCH_INTEGRATION=1 pytest -m twitch_live backend/tests/integration -v
    (or: RUN_TWITCH_INTEGRATION=1 python -m unittest backend.tests.integration.test_twitch_live -v)
Offline-only run:   pytest -m "not twitch_live"      (or: python -m unittest discover -s backend/tests -t .)

Nothing here is mocked. A test passes only if Twitch (or real stored Twitch events) actually answered.
For the full chat/EventSub round-trip use:  python -m scripts.verify_twitch
"""
import asyncio
import os
import unittest
from datetime import timedelta

try:  # lets `pytest -m twitch_live` select these; plain unittest works without pytest installed
    import pytest
    pytestmark = pytest.mark.twitch_live
except ImportError:
    pass

OPT_IN = os.environ.get("RUN_TWITCH_INTEGRATION") == "1"
SKIP_REASON = "needs real Twitch credentials + network: set RUN_TWITCH_INTEGRATION=1 (see docstring)"


@unittest.skipUnless(OPT_IN, SKIP_REASON)
class TwitchCredentialsAndNetwork(unittest.TestCase):
    def setUp(self):
        try:
            from backend import config
            from backend import twitch as tw
        except ImportError as e:  # httpx/sqlalchemy not installed
            self.skipTest(f"backend dependencies missing: {e}")
        if config.missing_config():
            self.skipTest(f"Twitch settings missing/placeholder: {config.missing_config()}")
        self.tw = tw

    def test_client_credentials_token_is_issued_by_twitch(self):
        token = asyncio.run(self.tw.app_token())
        self.assertTrue(isinstance(token, str) and len(token) >= 20)


@unittest.skipUnless(OPT_IN, SKIP_REASON)
class SlidingWindowOnRealCapturedEvents(unittest.TestCase):
    """Cross-checks the detector against the brute-force oracle using events that really came from Twitch."""

    def test_detector_matches_oracle_on_stored_twitch_events(self):
        try:
            from backend.db import Event, SessionLocal, utcnow
        except ImportError as e:
            self.skipTest(f"backend dependencies missing: {e}")
        from backend import highlights
        from backend.tests.test_sliding_window import brief, oracle
        since = utcnow() - timedelta(days=7)
        with SessionLocal() as db:
            rows = db.query(Event).filter(Event.source == "twitch", Event.created_at >= since).all()
        if not rows:
            self.skipTest("no real Twitch events stored yet; run the app connected to Twitch first")
        import json
        events = []
        for r in rows:
            try:
                viewers = json.loads(r.payload).get("viewers")
            except Exception:
                viewers = None
            events.append({"id": r.message_id, "type": r.type, "at": r.created_at, "viewers": viewers})
        got = highlights.candidates(events, utcnow(), live=False, limit=10 ** 6)
        self.assertEqual(brief(got), oracle(events))


if __name__ == "__main__":
    unittest.main()
