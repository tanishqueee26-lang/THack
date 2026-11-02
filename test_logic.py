"""Pure-logic tests (stdlib only). Events here are SYNTHETIC test inputs, not Twitch data."""
import unittest
from datetime import datetime, timedelta

from backend import highlights, quests

NOW = datetime(2026, 11, 2, 12, 0, 0)


def ev(t, secs_ago, **kw):
    return {"type": t, "at": NOW - timedelta(seconds=secs_ago), **kw}


class QuestTests(unittest.TestCase):
    def test_counts_only_matching_events_in_window(self):
        events = [ev("channel.follow", 60)] * 3 + [ev("channel.follow", 8 * 86400), ev("channel.subscribe", 60)]
        p = {q["id"]: q for q in quests.progress(events, NOW)}
        self.assertEqual(p["follower-rush"]["count"], 3)
        self.assertEqual(p["follower-rush"]["pct"], 30)
        self.assertEqual(p["sub-surge"]["count"], 1)
        self.assertFalse(p["raid-party"]["completed"])

    def test_completion_and_cap(self):
        p = {q["id"]: q for q in quests.progress([ev("channel.raid", 10)] * 2, NOW)}
        self.assertTrue(p["raid-party"]["completed"])
        self.assertEqual(p["raid-party"]["pct"], 100)

    def test_no_events_means_zero_progress(self):
        self.assertTrue(all(q["count"] == 0 and q["pct"] == 0 for q in quests.progress([], NOW)))


class HighlightTests(unittest.TestCase):
    def test_empty_gives_no_candidates(self):
        self.assertEqual(highlights.candidates([], NOW, live=True), [])

    def test_quiet_activity_is_not_a_highlight(self):
        self.assertEqual(highlights.candidates([ev("channel.follow", 30)], NOW, live=True), [])

    def test_raid_is_a_clippable_highlight_when_live_and_recent(self):
        c = highlights.candidates([ev("channel.raid", 20, viewers=50)], NOW, live=True)
        self.assertEqual(len(c), 1)
        self.assertTrue(c[0]["clippable"])
        self.assertIn("raid", c[0]["summary"])

    def test_not_clippable_when_offline_or_old(self):
        e = [ev("channel.raid", 20, viewers=50)]
        self.assertFalse(highlights.candidates(e, NOW, live=False)[0]["clippable"])
        old = [ev("channel.raid", 3600, viewers=50)]
        self.assertFalse(highlights.candidates(old, NOW, live=True)[0]["clippable"])

    def test_chat_burst_and_adjacent_buckets_merge(self):
        burst = [ev("channel.chat.message", 200 - i) for i in range(30)] + [ev("channel.chat.message", 130 - i) for i in range(30)]
        c = highlights.candidates(burst, NOW, live=True)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["reasons"]["channel.chat.message"], 60)


if __name__ == "__main__":
    unittest.main()
