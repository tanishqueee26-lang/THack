"""Sliding-window core for highlight detection. Pure logic: stdlib only, no I/O, no clock reads.

Semantics (all timestamps are naive UTC; aware datetimes are converted):
* A window is HALF-OPEN: [t, t + 60s). Two events exactly 60s apart are never in the same window.
* Candidate windows start at every distinct event timestamp. This is exhaustive: any 60s span's
  content is a subset of the window starting at its earliest event, and weights are non-negative.
* A window is "hot" when its score >= threshold. Hot windows whose spans overlap, or are separated
  by at most MERGE_GAP_S, form one highlight (a burst that pauses < 1 window is one highlight).
* Scores are INTEGER TENTHS of a point, so sums are exact and independent of arrival order.
* Events with an id are de-duplicated (earliest timestamp wins, ties broken by sort order, so the
  result never depends on input order). Events WITHOUT an id are never de-duplicated.
"""
import bisect
import math
from datetime import datetime, timedelta, timezone

WINDOW_S = 60
MERGE_GAP_S = 60
ALLOWED_LATENESS_S = 120
MAX_FUTURE_SKEW_S = 5
THRESHOLD = 8.0
CLIP_WINDOW = 90  # Create Clip captures up to ~90s of the live stream
WEIGHT_TENTHS = {"channel.raid": 100, "channel.subscribe": 50, "channel.follow": 20, "channel.chat.message": 3,
                  "quest.milestone": 50, "quest.completed": 70, "stream.marker": 40, "quest.participation": 6}
RAID_VIEWER_TENTHS = 1  # +0.1 point per raiding viewer
MAX_VIEWERS = 10_000_000
NAMES = {"channel.raid": "raid", "channel.subscribe": "sub", "channel.follow": "follow", "channel.chat.message": "chat msg", "quest.milestone": "milestone", "quest.completed": "quest complete",
         "stream.marker": "stream marker", "quest.participation": "participation"}


def points(e) -> int:
    """Event weight in tenths of a point (0 = ignored)."""
    base = WEIGHT_TENTHS.get(e.get("type"), 0)
    if base and e["type"] == "channel.raid":
        try:
            v = int(e.get("viewers") or 0)
        except (TypeError, ValueError, OverflowError):
            v = 0
        base += RAID_VIEWER_TENTHS * min(max(v, 0), MAX_VIEWERS)
    return base


def naive_utc(at):
    if not isinstance(at, datetime):
        return None
    return at.astimezone(timezone.utc).replace(tzinfo=None) if at.tzinfo else at


def normalize(events):
    """-> (records, stats). records = [(at, id, type, tenths)], sorted, de-duplicated, weight > 0 only."""
    stats = {"received": 0, "invalid": 0, "ignored": 0, "duplicates": 0, "used": 0}
    recs = []
    for e in events:
        stats["received"] += 1
        at = naive_utc(e.get("at")) if isinstance(e, dict) else None
        if at is None or not isinstance(e.get("type"), str):
            stats["invalid"] += 1
            continue
        p = points(e)
        if p <= 0:
            stats["ignored"] += 1
            continue
        recs.append((at, str(e["id"]) if e.get("id") not in (None, "") else "", e["type"], p))
    recs.sort()
    out, seen = [], set()
    for r in recs:
        if r[1]:
            if r[1] in seen:
                stats["duplicates"] += 1
                continue
            seen.add(r[1])
        out.append(r)
    stats["used"] = len(out)
    return out, stats


def dedupe(events):
    """Drop repeated deliveries (same non-empty id), keeping the earliest. Order-insensitive for ids."""
    first = {}
    for i, e in enumerate(events):
        eid = e.get("id")
        if eid in (None, ""):
            continue
        k = (naive_utc(e.get("at")) or datetime.min, i)
        if eid not in first or k < first[eid][0]:
            first[eid] = (k, i)
    keep = {v[1] for v in first.values()}
    return [e for i, e in enumerate(events) if e.get("id") in (None, "") or i in keep]


def scan(recs, threshold_tenths, window_s=WINDOW_S, merge_gap_s=MERGE_GAP_S):
    """O(n) two-pointer scan over sorted records. -> [{start, end, tenths, reasons, count}]."""
    n = len(recs)
    if not n:
        return []
    ts = [r[0] for r in recs]
    prefix = [0]
    for r in recs:
        prefix.append(prefix[-1] + r[3])
    win, gap = timedelta(seconds=window_s), timedelta(seconds=merge_gap_s)
    groups, i, j = [], 0, 0
    while i < n:
        while j < n and ts[j] < ts[i] + win:
            j += 1
        if prefix[j] - prefix[i] >= threshold_tenths:
            if groups and ts[i] - ts[groups[-1][1] - 1] <= gap:
                groups[-1][1] = max(groups[-1][1], j)
            else:
                groups.append([i, j])
        t = ts[i]
        while i < n and ts[i] == t:
            i += 1
    out = []
    for lo, hi in groups:
        reasons = {}
        for r in recs[lo:hi]:
            reasons[r[2]] = reasons.get(r[2], 0) + 1
        out.append({"start": ts[lo], "end": ts[hi - 1], "tenths": prefix[hi] - prefix[lo],
                    "reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))), "count": hi - lo})
    return out


def detect(recs, now, live, threshold=THRESHOLD, limit=10, window_s=WINDOW_S, merge_gap_s=MERGE_GAP_S):
    """Highlight candidates, ranked by score then start time (fully deterministic)."""
    thr = math.ceil(round(threshold * 10, 6))
    out = []
    for s in scan(recs, thr, window_s, merge_gap_s):
        age = (now - s["end"]).total_seconds()
        out.append({"start": s["start"].isoformat() + "Z", "end": s["end"].isoformat() + "Z",
                    "score": round(s["tenths"] / 10, 1), "reasons": s["reasons"], "event_count": s["count"],
                    "summary": ", ".join(f"{c} {NAMES.get(t, t)}" for t, c in s["reasons"].items()),
                    "clippable": bool(live) and 0 <= age <= CLIP_WINDOW})
    return sorted(out, key=lambda c: (-c["score"], c["start"]))[:max(0, limit)]


# Status values returned by SlidingWindow.ingest
ACCEPTED, DUPLICATE, TOO_LATE, IGNORED, INVALID, FUTURE = (
    "accepted", "duplicate", "too_late", "ignored", "invalid", "rejected_future")


class SlidingWindow:
    """Streaming detector: tolerates out-of-order and delayed events, rejects duplicates, evicts old data.

    Time model: an event-time WATERMARK = the newest timestamp seen (or the newest `now` passed to
    advance()/ingest()). An event older than watermark - allowed_lateness is dropped (TOO_LATE) and
    counted, never silently applied. Retention = window + allowed_lateness, so memory is bounded and
    a duplicate of an evicted event is TOO_LATE rather than double-counted.
    `now=` (wall clock) is optional; only when given are far-future timestamps rejected.
    """

    def __init__(self, window_s=WINDOW_S, allowed_lateness_s=ALLOWED_LATENESS_S,
                 max_future_skew_s=MAX_FUTURE_SKEW_S, merge_gap_s=MERGE_GAP_S):
        self.window = timedelta(seconds=window_s)
        self.lateness = timedelta(seconds=allowed_lateness_s)
        self.skew = timedelta(seconds=max_future_skew_s)
        self.merge_gap_s, self.window_s = merge_gap_s, window_s
        self._recs = []  # sorted: (at, id, type, tenths, seq)
        self._ids = set()
        self._seq = 0
        self.watermark = None
        self.stats = {ACCEPTED: 0, DUPLICATE: 0, TOO_LATE: 0, IGNORED: 0, INVALID: 0, FUTURE: 0, "evicted": 0}

    def __len__(self):
        return len(self._recs)

    @property
    def known_ids(self) -> int:
        return len(self._ids)

    def _evict(self) -> int:
        if self.watermark is None:
            return 0
        horizon = self.watermark - (self.window + self.lateness)
        cut = bisect.bisect_left(self._recs, (horizon,))
        for r in self._recs[:cut]:
            self._ids.discard(r[1])
        del self._recs[:cut]
        self.stats["evicted"] += cut
        return cut

    def advance(self, now) -> int:
        """Move the watermark forward with the wall clock (never backwards). Returns events evicted."""
        now = naive_utc(now)
        if now is not None and (self.watermark is None or now > self.watermark):
            self.watermark = now
        return self._evict()

    def _result(self, status):
        self.stats[status] += 1
        return status

    def ingest(self, event, now=None) -> str:
        if now is not None:
            self.advance(now)
        at = naive_utc(event.get("at")) if isinstance(event, dict) else None
        if at is None or not isinstance(event.get("type"), str):
            return self._result(INVALID)
        p = points(event)
        if p <= 0:
            return self._result(IGNORED)
        eid = str(event["id"]) if event.get("id") not in (None, "") else ""
        if eid and eid in self._ids:
            return self._result(DUPLICATE)
        if now is not None and at > naive_utc(now) + self.skew:
            return self._result(FUTURE)
        if self.watermark is not None and at < self.watermark - self.lateness:
            return self._result(TOO_LATE)
        self._seq += 1
        bisect.insort(self._recs, (at, eid, event["type"], p, self._seq))
        if eid:
            self._ids.add(eid)
        if self.watermark is None or at > self.watermark:
            self.watermark = at
        self._evict()
        return self._result(ACCEPTED)

    def active(self, now=None) -> dict:
        """Events in the trailing window (ref - 60s, ref]; zeros when empty."""
        ref = naive_utc(now) if now is not None else self.watermark
        if ref is None:
            return {"window_seconds": self.window_s, "as_of": None, "count": 0, "score": 0.0, "by_type": {},
                    "start": None, "end": None}
        lo = ref - self.window
        picked = [r for r in self._recs[bisect.bisect_left(self._recs, (lo,)):] if lo < r[0] <= ref]
        by_type = {}
        for r in picked:
            by_type[r[2]] = by_type.get(r[2], 0) + 1
        return {"window_seconds": self.window_s, "as_of": ref.isoformat() + "Z", "count": len(picked),
                "score": round(sum(r[3] for r in picked) / 10, 1),
                "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
                "start": picked[0][0].isoformat() + "Z" if picked else None,
                "end": picked[-1][0].isoformat() + "Z" if picked else None}

    def candidates(self, now, live, threshold=THRESHOLD, limit=10):
        now = naive_utc(now)
        return detect([r[:4] for r in self._recs], now, live, threshold, limit, self.window_s, self.merge_gap_s)
