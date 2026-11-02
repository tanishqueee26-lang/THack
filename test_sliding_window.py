"""Offline tests for the sliding-window detector (stdlib only). All events are SYNTHETIC test inputs."""
import json
import os
import random
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import highlights, quests, window
from backend.window import ACCEPTED, DUPLICATE, FUTURE, IGNORED, INVALID, TOO_LATE, SlidingWindow

T0 = datetime(2026, 11, 2, 11, 59, 0)  # a minute boundary (12:00:00) sits 60s after this
BOUNDARY = datetime(2026, 11, 2, 12, 0, 0)
NOW = datetime(2026, 11, 2, 12, 30, 0)
CHAT = "channel.chat.message"


def chat(at, i=None, **kw):
    e = {"type": CHAT, "at": at, **kw}
    if i is not None:
        e["id"] = f"m{i}"
    return e


def burst(start, n, spacing_ms, prefix="b", typ=CHAT):
    return [{"type": typ, "at": start + timedelta(milliseconds=k * spacing_ms), "id": f"{prefix}{k}"} for k in range(n)]


def cands(events, **kw):
    return highlights.candidates(events, NOW, live=False, **kw)


def oracle(events, threshold=8.0, win=60, gap=60):
    """Independent O(n^2) implementation of the stated semantics, used to cross-check the fast scan."""
    evs = sorted({(e["at"], e["type"], window.points(e), e.get("id", "")) for e in events if window.points(e) > 0})
    thr, W, G = round(threshold * 10), timedelta(seconds=win), timedelta(seconds=gap)
    hot = []
    for t in sorted({e[0] for e in evs}):
        inside = [e for e in evs if t <= e[0] < t + W]
        if sum(e[2] for e in inside) >= thr:
            hot.append((t, max(e[0] for e in inside)))
    groups = []
    for first, last in hot:
        if groups and first - groups[-1][1] <= G:
            groups[-1][1] = max(groups[-1][1], last)
        else:
            groups.append([first, last])
    out = []
    for first, last in groups:
        members = [e for e in evs if first <= e[0] <= last]
        out.append({"start": first.isoformat() + "Z", "end": last.isoformat() + "Z",
                    "score": round(sum(e[2] for e in members) / 10, 1), "count": len(members)})
    return sorted(out, key=lambda c: (-c["score"], c["start"]))


def brief(cs):
    return [{k: c[k] for k in ("start", "end", "score")} | {"count": c["event_count"]} for c in cs]


class WindowSemantics(unittest.TestCase):
    def test_window_is_half_open_60s(self):
        # 14 msgs = 4.2 points each side; together 8.4 >= 8 only if they share a window
        a = burst(BOUNDARY, 14, 0, "a")  # all at one instant, so the 60s boundary is unambiguous
        inside = burst(BOUNDARY + timedelta(seconds=59, milliseconds=999), 14, 0, "c")
        exact = burst(BOUNDARY + timedelta(seconds=60), 14, 0, "d")
        self.assertEqual(len(cands(a + inside)), 1)
        self.assertEqual(cands(a + exact), [])  # exactly 60s apart: different windows

    def test_threshold_is_inclusive_and_exact(self):
        self.assertEqual(len(cands(burst(BOUNDARY, 27, 100))), 1)  # 27 * 0.3 = 8.1 >= 8.0
        self.assertEqual(cands(burst(BOUNDARY, 26, 100)), [])      # 7.8 < 8.0
        self.assertEqual(cands([{"type": "channel.raid", "at": BOUNDARY, "viewers": 0}])[0]["score"], 10.0)  # a raid alone is a highlight
        self.assertEqual(cands([{"type": "channel.follow", "at": BOUNDARY, "id": "f"}]), [])               # 2.0 < 8.0

    def test_score_is_exact_no_float_drift(self):
        c = cands(burst(BOUNDARY, 30, 100))[0]
        self.assertEqual(c["score"], 9.0)
        self.assertEqual(cands(burst(BOUNDARY, 3000, 5))[0]["score"], 900.0)

    def test_candidate_span_and_reasons(self):
        c = cands(burst(BOUNDARY, 30, 100) + [{"type": "channel.follow", "at": BOUNDARY + timedelta(seconds=5), "id": "f"}])[0]
        self.assertEqual(c["reasons"], {CHAT: 30, "channel.follow": 1})
        self.assertEqual(c["start"], "2026-11-02T12:00:00Z")
        self.assertEqual(c["event_count"], 31)

    def test_raid_scoring_and_clippable(self):
        raid = [{"type": "channel.raid", "at": NOW - timedelta(seconds=20), "viewers": 50, "id": "r"}]
        c = highlights.candidates(raid, NOW, live=True)[0]
        self.assertEqual(c["score"], 15.0)
        self.assertTrue(c["clippable"])
        self.assertFalse(highlights.candidates(raid, NOW, live=False)[0]["clippable"])
        self.assertFalse(highlights.candidates(raid, NOW + timedelta(seconds=200), live=True)[0]["clippable"])
        self.assertFalse(highlights.candidates(raid, NOW - timedelta(seconds=60), live=True)[0]["clippable"])  # future

    def test_bad_viewer_values_do_not_crash(self):
        for v in (None, "x", float("inf"), -5, 10 ** 30, True):
            highlights.candidates([{"type": "channel.raid", "at": BOUNDARY, "viewers": v}], NOW, live=True)

    def test_aware_datetimes_are_normalised_to_naive_utc(self):
        aware = [{"type": CHAT, "at": e["at"].replace(tzinfo=timezone.utc), "id": e["id"]} for e in burst(BOUNDARY, 30, 100)]
        self.assertEqual(cands(aware), cands(burst(BOUNDARY, 30, 100)))


class MinuteBoundaryBursts(unittest.TestCase):
    def test_burst_straddling_the_minute_is_one_highlight(self):
        ev = burst(BOUNDARY - timedelta(seconds=15), 40, 375, "pre") + burst(BOUNDARY, 40, 375, "post")
        c = cands(ev)
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0]["event_count"], c[0]["score"]), (80, 24.0))
        self.assertLess(datetime.fromisoformat(c[0]["start"][:-1]), BOUNDARY)
        self.assertGreater(datetime.fromisoformat(c[0]["end"][:-1]), BOUNDARY)

    def test_split_burst_is_hot_only_because_the_window_slides(self):
        ev = burst(BOUNDARY - timedelta(seconds=10), 15, 600, "pre") + burst(BOUNDARY, 15, 600, "post")
        per_minute = {}
        for e in ev:
            k = e["at"].replace(second=0, microsecond=0)
            per_minute[k] = per_minute.get(k, 0) + window.points(e)
        self.assertTrue(all(v < 80 for v in per_minute.values()), per_minute)  # fixed minute buckets miss it
        c = cands(ev)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["score"], 9.0)

    def test_every_phase_offset_of_a_burst_is_detected(self):
        for off in range(0, 60):  # slide a 30-msg burst across the whole minute boundary
            start = BOUNDARY - timedelta(seconds=off)
            self.assertEqual(len(cands(burst(start, 30, 500))), 1, off)

    def test_two_bursts_far_apart_stay_separate_and_ranked(self):
        a, b = burst(BOUNDARY, 30, 100, "a"), burst(BOUNDARY + timedelta(minutes=10), 60, 100, "b")
        c = cands(a + b)
        self.assertEqual([x["score"] for x in c], [18.0, 9.0])

    def test_pause_shorter_than_merge_gap_merges_longer_does_not(self):
        first = burst(BOUNDARY, 30, 100, "a")
        near = burst(BOUNDARY + timedelta(seconds=40), 30, 100, "b")   # ~37s after first ends
        far = burst(BOUNDARY + timedelta(seconds=200), 30, 100, "c")
        self.assertEqual(len(cands(first + near)), 1)
        self.assertEqual(len(cands(first + far)), 2)

    def test_midnight_and_day_boundary(self):
        mid = datetime(2026, 11, 3, 0, 0, 0)
        c = cands(burst(mid - timedelta(seconds=15), 60, 500))
        self.assertEqual(len(c), 1)


class IdenticalTimestamps(unittest.TestCase):
    def test_many_events_at_one_instant(self):
        ev = [chat(BOUNDARY, i) for i in range(30)]
        c = cands(ev)
        self.assertEqual((len(c), c[0]["event_count"], c[0]["score"]), (1, 30, 9.0))
        self.assertEqual(c[0]["start"], c[0]["end"])

    def test_order_of_identical_timestamps_is_irrelevant(self):
        ev = [chat(BOUNDARY, i) for i in range(30)] + [{"type": "channel.follow", "at": BOUNDARY, "id": "f"}]
        base = json.dumps(cands(ev), sort_keys=True)
        rng = random.Random(7)
        for _ in range(25):
            rng.shuffle(ev)
            self.assertEqual(json.dumps(cands(ev), sort_keys=True), base)

    def test_identical_timestamps_without_ids_all_count(self):
        ev = [{"type": CHAT, "at": BOUNDARY}] * 30  # no ids => distinct deliveries
        self.assertEqual(cands(ev)[0]["event_count"], 30)

    def test_identical_timestamp_and_id_counts_once(self):
        ev = [chat(BOUNDARY, 1)] * 30
        self.assertEqual(cands(ev), [])  # one message = 0.3

    def test_mixed_types_at_same_instant(self):
        ev = [{"type": "channel.raid", "at": BOUNDARY, "viewers": 0, "id": "r"},
              {"type": "channel.follow", "at": BOUNDARY, "id": "f"}]
        c = cands(ev)[0]
        self.assertEqual((c["score"], c["reasons"]), (12.0, {"channel.follow": 1, "channel.raid": 1}))

    def test_equal_scores_rank_by_start_time(self):
        a = [chat(BOUNDARY + timedelta(minutes=20), f"x{i}") for i in range(30)]
        b = [chat(BOUNDARY, f"y{i}") for i in range(30)]
        c = cands(a + b)
        self.assertEqual([x["start"] for x in c], ["2026-11-02T12:00:00Z", "2026-11-02T12:20:00Z"])

    def test_streaming_ties_are_stable(self):
        sw = SlidingWindow()
        for e in [chat(BOUNDARY, i) for i in range(30)]:
            self.assertEqual(sw.ingest(e), ACCEPTED)
        self.assertEqual(sw.active()["count"], 30)


class OutOfOrderAndDuplicates(unittest.TestCase):
    def test_batch_is_order_insensitive(self):
        ev = burst(BOUNDARY, 40, 700, "a") + burst(BOUNDARY + timedelta(minutes=5), 40, 700, "b")
        ref = cands(sorted(ev, key=lambda e: e["at"]))
        rng = random.Random(1)
        for _ in range(20):
            rng.shuffle(ev)
            self.assertEqual(cands(ev), ref)
        self.assertEqual(cands(list(reversed(sorted(ev, key=lambda e: e["at"])))), ref)

    def test_duplicates_counted_once_regardless_of_order(self):
        ev = burst(BOUNDARY, 30, 100)
        doubled = ev + [dict(e) for e in ev] + [dict(e) for e in ev[:10]]
        random.Random(3).shuffle(doubled)
        self.assertEqual(cands(doubled), cands(ev))
        recs, stats = window.normalize(doubled)
        self.assertEqual((stats["duplicates"], stats["used"]), (40, 30))

    def test_same_id_with_different_timestamps_keeps_earliest_deterministically(self):
        e1, e2 = chat(BOUNDARY, 1), chat(BOUNDARY + timedelta(seconds=5), 1)
        for order in ([e1, e2], [e2, e1]):
            recs, _ = window.normalize(order)
            self.assertEqual(recs[0][0], BOUNDARY)

    def test_quests_do_not_double_count_repeat_deliveries(self):
        f = {"type": "channel.follow", "at": NOW - timedelta(seconds=10), "id": "dup"}
        p = {q["id"]: q for q in quests.progress([f, dict(f), dict(f)], NOW)}
        self.assertEqual(p["follower-rush"]["count"], 1)
        nid = [{"type": "channel.follow", "at": NOW - timedelta(seconds=10)}] * 3  # no ids: all count
        self.assertEqual({q["id"]: q for q in quests.progress(nid, NOW)}["follower-rush"]["count"], 3)

    def test_streaming_duplicate_rejected(self):
        sw = SlidingWindow()
        self.assertEqual(sw.ingest(chat(BOUNDARY, 1)), ACCEPTED)
        self.assertEqual(sw.ingest(chat(BOUNDARY, 1)), DUPLICATE)
        self.assertEqual(sw.ingest(chat(BOUNDARY + timedelta(seconds=1), 1)), DUPLICATE)  # id wins over timestamp
        self.assertEqual((len(sw), sw.stats[DUPLICATE]), (1, 2))

    def test_streaming_out_of_order_within_lateness_is_accepted_and_sorted(self):
        sw = SlidingWindow()
        times = [10, 3, 8, 1, 9, 2, 7]
        for i, s in enumerate(times):
            self.assertEqual(sw.ingest(chat(BOUNDARY + timedelta(seconds=s), i)), ACCEPTED)
        self.assertEqual([r[0] for r in sw._recs], sorted(BOUNDARY + timedelta(seconds=s) for s in times))

    def test_streaming_matches_batch_for_any_arrival_order(self):
        ev = burst(BOUNDARY, 50, 400, "a") + burst(BOUNDARY + timedelta(seconds=45), 30, 400, "b")
        ref = cands(ev)
        for seed in range(10):
            shuffled = ev[:]
            random.Random(seed).shuffle(shuffled)
            sw = SlidingWindow(allowed_lateness_s=3600)
            for e in shuffled:
                sw.ingest(e)
            self.assertEqual(sw.candidates(NOW, False), ref, seed)


class EmptyAndDelayed(unittest.TestCase):
    def test_empty_inputs(self):
        self.assertEqual(cands([]), [])
        self.assertEqual(highlights.candidates([], NOW, live=True), [])
        sw = SlidingWindow()
        a = sw.active()
        self.assertEqual((a["count"], a["score"], a["by_type"], a["start"]), (0, 0.0, {}, None))
        self.assertEqual(sw.candidates(NOW, True), [])
        self.assertEqual(sw.active(NOW)["count"], 0)

    def test_weightless_and_invalid_events_are_counted_not_used(self):
        sw = SlidingWindow()
        self.assertEqual(sw.ingest({"type": "channel.unknown", "at": BOUNDARY}), IGNORED)
        self.assertEqual(sw.ingest({"type": CHAT}), INVALID)
        self.assertEqual(sw.ingest({"at": BOUNDARY}), INVALID)
        self.assertEqual(sw.ingest({"type": CHAT, "at": "2026-11-02"}), INVALID)
        self.assertEqual(len(sw), 0)
        self.assertEqual(cands([{"type": "channel.unknown", "at": BOUNDARY}, {"type": CHAT}]), [])

    def test_gap_in_activity_gives_empty_active_window(self):
        sw = SlidingWindow()
        for e in burst(BOUNDARY, 30, 100):
            sw.ingest(e)
        self.assertEqual(sw.active(BOUNDARY + timedelta(seconds=10))["count"], 30)
        self.assertEqual(sw.active(BOUNDARY + timedelta(seconds=63))["count"], 0)   # all > 60s old
        self.assertEqual(sw.active(BOUNDARY + timedelta(seconds=63))["score"], 0.0)

    def test_active_window_edges(self):
        sw = SlidingWindow()
        sw.ingest(chat(BOUNDARY, 1))
        self.assertEqual(sw.active(BOUNDARY + timedelta(seconds=59, milliseconds=999))["count"], 1)
        self.assertEqual(sw.active(BOUNDARY + timedelta(seconds=60))["count"], 0)       # exactly 60s old: out
        self.assertEqual(sw.active(BOUNDARY)["count"], 1)                                  # event at 'now': in

    def test_delayed_event_within_lateness_completes_a_highlight(self):
        sw = SlidingWindow()
        for e in burst(BOUNDARY, 20, 500, "a"):                      # 6.0 points: below threshold
            sw.ingest(e, now=BOUNDARY + timedelta(seconds=10))
        self.assertEqual(sw.candidates(BOUNDARY + timedelta(seconds=40), False), [])
        late = burst(BOUNDARY + timedelta(seconds=2), 10, 500, "late")   # delivered 38s late
        for e in late:
            self.assertEqual(sw.ingest(e, now=BOUNDARY + timedelta(seconds=40)), ACCEPTED)
        c = sw.candidates(BOUNDARY + timedelta(seconds=40), False)
        self.assertEqual((len(c), c[0]["event_count"], c[0]["score"]), (1, 30, 9.0))

    def test_event_older_than_allowed_lateness_is_dropped_and_counted(self):
        sw = SlidingWindow(allowed_lateness_s=120)
        sw.ingest(chat(BOUNDARY + timedelta(minutes=10), 1))
        self.assertEqual(sw.ingest(chat(BOUNDARY, 2)), TOO_LATE)
        self.assertEqual(sw.ingest(chat(BOUNDARY + timedelta(minutes=8, seconds=1), 3)), ACCEPTED)  # 119s late: ok
        self.assertEqual(sw.stats[TOO_LATE], 1)
        self.assertEqual(len(sw), 2)

    def test_far_future_timestamp_rejected_only_with_wall_clock(self):
        sw = SlidingWindow()
        self.assertEqual(sw.ingest(chat(BOUNDARY + timedelta(hours=1), 1), now=BOUNDARY), FUTURE)
        self.assertEqual(sw.ingest(chat(BOUNDARY + timedelta(seconds=3), 2), now=BOUNDARY), ACCEPTED)  # within skew
        self.assertEqual(len(sw), 1)
        self.assertEqual(sw.watermark, BOUNDARY + timedelta(seconds=3))  # the rejected event did not move the clock

    def test_watermark_never_moves_backwards(self):
        sw = SlidingWindow()
        sw.advance(BOUNDARY + timedelta(minutes=5))
        sw.advance(BOUNDARY)
        self.assertEqual(sw.watermark, BOUNDARY + timedelta(minutes=5))


class Eviction(unittest.TestCase):
    def test_old_events_leave_the_active_window_and_memory(self):
        sw = SlidingWindow(allowed_lateness_s=120)
        for k in range(600):                                           # 10 minutes, 1 msg/s
            sw.ingest(chat(BOUNDARY + timedelta(seconds=k), k))
        horizon = sw.watermark - timedelta(seconds=180)
        self.assertGreaterEqual(sw._recs[0][0], horizon)               # nothing older than window+lateness
        self.assertLessEqual(len(sw), 181)
        self.assertEqual(sw.known_ids, len(sw))                        # dedupe memory is bounded too
        self.assertEqual(sw.active()["count"], 60)
        self.assertEqual(sw.stats["evicted"], 600 - len(sw))

    def test_advance_clears_everything_after_silence(self):
        sw = SlidingWindow()
        for e in burst(BOUNDARY, 30, 100):
            sw.ingest(e)
        self.assertEqual(sw.advance(BOUNDARY + timedelta(hours=1)), 30)
        self.assertEqual((len(sw), sw.known_ids), (0, 0))
        self.assertEqual(sw.active()["count"], 0)
        self.assertEqual(sw.candidates(BOUNDARY + timedelta(hours=1), True), [])

    def test_replay_of_an_evicted_event_is_not_double_counted(self):
        sw = SlidingWindow()
        e = chat(BOUNDARY, 1)
        sw.ingest(e)
        sw.advance(BOUNDARY + timedelta(hours=1))
        self.assertEqual(sw.ingest(dict(e)), TOO_LATE)
        self.assertEqual(len(sw), 0)

    def test_evicted_events_no_longer_produce_highlights(self):
        sw = SlidingWindow()
        for e in burst(BOUNDARY, 30, 100):
            sw.ingest(e)
        self.assertEqual(len(sw.candidates(BOUNDARY + timedelta(seconds=20), False)), 1)
        sw.advance(BOUNDARY + timedelta(seconds=20 + 181))
        self.assertEqual(sw.candidates(BOUNDARY + timedelta(seconds=201), False), [])


class Determinism(unittest.TestCase):
    EVENTS = (burst(BOUNDARY, 40, 300, "a") + burst(BOUNDARY, 20, 0, "same")
              + [{"type": "channel.raid", "at": BOUNDARY + timedelta(minutes=3), "viewers": 12, "id": "r"},
                 {"type": "channel.subscribe", "at": BOUNDARY + timedelta(minutes=3), "id": "s"}])

    def test_repeated_runs_identical(self):
        first = json.dumps(cands(self.EVENTS), sort_keys=False)
        for _ in range(10):
            self.assertEqual(json.dumps(cands(self.EVENTS), sort_keys=False), first)

    def test_output_key_order_is_stable(self):
        c = cands(self.EVENTS)[0]
        self.assertEqual(list(c), ["start", "end", "score", "reasons", "event_count", "summary", "clippable"])
        counts = list(c["reasons"].values())
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_identical_across_python_hash_seeds(self):
        code = ("import json,sys;from datetime import datetime,timedelta;from backend import highlights\n"
                "B=datetime(2026,11,2,12);ev=[{'type':'channel.chat.message','at':B+timedelta(milliseconds=300*k),'id':f'a{k}'} for k in range(40)]\n"
                "ev+=[{'type':'channel.raid','at':B+timedelta(minutes=3),'viewers':12,'id':'r'},{'type':'channel.follow','at':B+timedelta(minutes=3),'id':'f'}]\n"
                "print(json.dumps(highlights.candidates(ev,B+timedelta(hours=1),True)))")
        root = Path(__file__).resolve().parents[2]
        outs = set()
        for seed in ("0", "1", "42", "random"):
            r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                               env={**os.environ, "PYTHONHASHSEED": seed})
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.add(r.stdout)
        self.assertEqual(len(outs), 1)


def synthetic_stream(rng, n, seconds, types=(CHAT,), burst_prob=0.15):
    """n events over `seconds` with random bursts; unique ids."""
    centers = [rng.uniform(0, seconds) for _ in range(max(1, int(seconds * burst_prob / 30)))]
    out = []
    for i in range(n):
        t = rng.choice(centers) + rng.gauss(0, 8) if rng.random() < 0.6 else rng.uniform(0, seconds)
        out.append({"type": rng.choice(types), "at": BOUNDARY + timedelta(seconds=min(max(t, 0), seconds)),
                    "id": f"e{i}", "viewers": rng.randint(0, 30)})
    return out


class HighVolume(unittest.TestCase):
    def test_fast_scan_matches_bruteforce_oracle_on_random_streams(self):
        types = (CHAT, CHAT, CHAT, "channel.follow", "channel.subscribe", "channel.raid")
        for seed in range(12):
            rng = random.Random(seed)
            ev = synthetic_stream(rng, rng.randint(50, 500), rng.choice((120, 600, 1800)), types)
            for thr in (3.0, 8.0, 25.0):
                self.assertEqual(brief(cands(ev, threshold=thr, limit=10 ** 6)), oracle(ev, thr), (seed, thr))

    def test_200k_chat_messages_in_batch(self):
        rng = random.Random(2026)
        ev = synthetic_stream(rng, 200_000, 3600)
        t = time.perf_counter()
        c = cands(ev, limit=5)
        elapsed = time.perf_counter() - t
        self.assertLess(elapsed, 20, f"took {elapsed:.1f}s; scan must be near-linear")
        self.assertTrue(0 < len(c) <= 5)
        self.assertEqual([x["score"] for x in c], sorted((x["score"] for x in c), reverse=True))

    def test_100k_messages_streamed_out_of_order_stay_bounded_and_correct(self):
        rng = random.Random(99)
        truth = [{"type": CHAT, "at": BOUNDARY + timedelta(milliseconds=36 * i), "id": f"h{i}"} for i in range(100_000)]
        arrival = sorted(truth, key=lambda e: e["at"] + timedelta(seconds=rng.uniform(0, 30)))  # up to 30s late
        sw, peak = SlidingWindow(allowed_lateness_s=120), 0
        statuses = {}
        for e in arrival:
            s = sw.ingest(e)
            statuses[s] = statuses.get(s, 0) + 1
            peak = max(peak, len(sw))
        self.assertEqual(statuses, {ACCEPTED: 100_000})
        self.assertLess(peak, 7000)                                  # ~180s * 27.8/s ≈ 5000, never all 100k
        self.assertEqual(sw.known_ids, len(sw))
        end = max(e["at"] for e in truth)
        expect = sum(1 for e in truth if end - timedelta(seconds=60) < e["at"] <= end)
        self.assertEqual(sw.active()["count"], expect)
        self.assertEqual(sw.active()["score"], round(expect * 0.3, 1))

    def test_streaming_candidates_equal_batch_on_recent_history(self):
        rng = random.Random(5)
        ev = synthetic_stream(rng, 5000, 170)                          # all within retention
        sw = SlidingWindow(allowed_lateness_s=3600)                    # lateness covers the full shuffle: nothing dropped
        arrival = ev[:]
        rng.shuffle(arrival)
        self.assertEqual({sw.ingest(e) for e in arrival}, {ACCEPTED})
        self.assertEqual(sw.candidates(NOW, False, limit=100), cands(ev, limit=100))

    def test_events_later_than_allowed_lateness_are_dropped_and_reported_not_applied(self):
        rng = random.Random(5)
        ev = synthetic_stream(rng, 5000, 170)
        arrival = ev[:]
        rng.shuffle(arrival)                                           # up to ~170s out of order
        sw = SlidingWindow(allowed_lateness_s=120)
        statuses = [sw.ingest(e) for e in arrival]
        dropped = statuses.count(TOO_LATE)
        self.assertGreater(dropped, 0)
        self.assertEqual(statuses.count(ACCEPTED) + dropped, len(ev))
        self.assertEqual(sw.stats[TOO_LATE], dropped)                  # every drop is accounted for

    def test_sustained_chat_spam_is_one_merged_highlight(self):
        c = cands(burst(BOUNDARY, 6000, 100, "spam"))                  # 10 msg/s for 10 minutes
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0]["event_count"], c[0]["score"]), (6000, 1800.0))


if __name__ == "__main__":
    unittest.main()
