"""Keeps posting at a human pace so you don't trip spam filters or flood a community."""

from __future__ import annotations

from datetime import datetime, timedelta

from wingman.config import PacingSettings
from wingman.core.models import now_utc
from wingman.core.store import Store


def next_allowed(store: Store, platform: str, source: str, rules: PacingSettings,
                 now: datetime | None = None) -> datetime | None:
    """None if posting now is fine, else the earliest time it would be."""
    now = now or now_utc()
    day_ago, hour_ago = now - timedelta(days=1), now - timedelta(hours=1)
    last_day = sorted(store.posted_since(platform, day_ago))
    last_hour = [t for t in last_day if t >= hour_ago]
    source_day = sorted(store.posted_since(platform, day_ago, source))

    candidates: list[datetime] = []
    if last_day and last_day[-1] + timedelta(minutes=rules.min_gap_minutes) > now:
        candidates.append(last_day[-1] + timedelta(minutes=rules.min_gap_minutes))
    # When at a limit, the slot frees up when the oldest post in the window ages out.
    if len(last_hour) >= rules.max_per_hour:
        candidates.append(last_hour[-rules.max_per_hour] + timedelta(hours=1))
    if len(last_day) >= rules.max_per_day:
        candidates.append(last_day[-rules.max_per_day] + timedelta(days=1))
    if len(source_day) >= rules.max_per_source_per_day:
        candidates.append(source_day[-rules.max_per_source_per_day] + timedelta(days=1))
    return max(candidates) if candidates else None
