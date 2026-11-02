"""Highlight candidates from real stored events. Thin wrapper over backend.window (true sliding 60s window).
Explainable heuristic: weight events, slide a half-open 60s window over every event timestamp, merge
neighbouring hot windows. Timestamps are naive UTC."""
from backend import window

WEIGHTS = {t: v / 10 for t, v in window.WEIGHT_TENTHS.items()}
THRESHOLD, CLIP_WINDOW, NAMES = window.THRESHOLD, window.CLIP_WINDOW, window.NAMES


def score_event(e) -> float:
    return window.points(e) / 10


def candidates(events, now, live: bool, threshold: float = THRESHOLD, limit: int = 10):
    """events: iterable of {"type", "at", optional "id", optional "viewers"}. Order-insensitive and deterministic."""
    recs, _ = window.normalize(events)
    return window.detect(recs, now, live, threshold, limit)
