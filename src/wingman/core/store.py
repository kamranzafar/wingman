"""SQLite persistence: seen threads, rankings, the post log/queue, and activity snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from wingman.core.models import ActivityItem, Comment, RankedThread, Thread, now_utc
from wingman.core.quality import Rating

ThreadStatus = Literal["ranked", "pasted", "skipped", "queued", "posted"]
PostStatus = Literal["queued", "posted", "failed"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source TEXT NOT NULL,
    data TEXT NOT NULL,
    relevance REAL,
    opportunity REAL,
    final REAL,
    angle TEXT,
    status TEXT NOT NULL DEFAULT 'ranked',   -- ranked | pasted | skipped | queued | posted
    scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    source TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,                    -- queued | posted | failed
    due_at TEXT,
    posted_at TEXT,
    url TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    platform TEXT NOT NULL,
    id TEXT NOT NULL,
    source TEXT NOT NULL,
    body TEXT NOT NULL,
    score INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    permalink TEXT,
    PRIMARY KEY (platform, id)
);
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    outcome TEXT NOT NULL,                   -- posted | queued | skipped | abandoned
    similarity REAL,                         -- draft vs what you posted, 0-1
    regenerations INTEGER NOT NULL DEFAULT 0,
    issues TEXT NOT NULL DEFAULT '[]',       -- JSON list: facts, voice, relevance, length, other
    at TEXT NOT NULL
);
"""


def _thread_to_json(thread: Thread) -> str:
    d = asdict(thread)
    d["created_at"] = thread.created_at.isoformat()
    return json.dumps(d)


def _thread_from_json(data: str) -> Thread:
    d = json.loads(data)
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    d["top_comments"] = [Comment(**c) for c in d.get("top_comments", [])]
    return Thread(**d)


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # threads -------------------------------------------------------------
    def known_keys(self, platform: str, acted_on_only: bool = False) -> set[str]:
        """Threads already stored; with acted_on_only, just those you've moved past 'ranked' (skipped, posted…)."""
        sql = "SELECT key FROM threads WHERE platform = ?" + (" AND status != 'ranked'" if acted_on_only else "")
        return {r["key"] for r in self.db.execute(sql, (platform,))}

    def _upsert_thread(self, thread: Thread, status: ThreadStatus, relevance=None, opportunity=None,
                       final=None, angle: str = "") -> None:
        # A thread you've already replied to stays 'posted', so the duplicate-reply warning keeps working.
        with self.db:
            self.db.execute(
                """INSERT INTO threads (key, platform, source, data, relevance, opportunity, final, angle,
                                        status, scanned_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       source = excluded.source, data = excluded.data, relevance = excluded.relevance,
                       opportunity = excluded.opportunity, final = excluded.final, angle = excluded.angle,
                       scanned_at = excluded.scanned_at,
                       status = CASE WHEN threads.status = 'posted' THEN 'posted' ELSE excluded.status END""",
                (thread.key, thread.platform, thread.source, _thread_to_json(thread), relevance, opportunity,
                 final, angle, status, now_utc().isoformat()),
            )

    def save_ranked(self, r: RankedThread) -> None:
        self._upsert_thread(r.thread, "ranked", r.relevance, r.opportunity, r.final, r.angle)

    def save_pasted(self, thread: Thread) -> None:
        """A thread you brought in with `wingman draft`; kept out of the scan/review queue."""
        self._upsert_thread(thread, "pasted")

    def review_queue(self, platform: str, min_final: float, max_age_hours: float, limit: int) -> list[RankedThread]:
        rows = self.db.execute(
            "SELECT * FROM threads WHERE platform = ? AND status = 'ranked' AND final >= ? ORDER BY final DESC",
            (platform, min_final),
        )
        now, out = now_utc(), []
        for r in rows:
            t = _thread_from_json(r["data"])
            if t.age_hours(now) <= max_age_hours:
                out.append(RankedThread(t, r["relevance"], r["opportunity"], r["final"], r["angle"] or ""))
            if len(out) >= limit:
                break
        return out

    def set_status(self, key: str, status: ThreadStatus) -> None:
        with self.db:
            self.db.execute("UPDATE threads SET status = ? WHERE key = ?", (status, key))

    def thread_status(self, key: str) -> ThreadStatus | None:
        row = self.db.execute("SELECT status FROM threads WHERE key = ?", (key,)).fetchone()
        return row["status"] if row else None

    # posts ---------------------------------------------------------------
    def record_post(self, platform: str, source: str, thread_key: str, text: str, status: PostStatus,
                    due_at: datetime | None = None, url: str | None = None, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO posts (platform, source, thread_key, text, status, due_at, posted_at, url, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (platform, source, thread_key, text, status, due_at.isoformat() if due_at else None,
                 now_utc().isoformat() if status == "posted" else None, url, error),
            )

    def mark_post(self, post_id: int, status: PostStatus, url: str | None = None, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE posts SET status = ?, url = ?, error = ?, posted_at = ? WHERE id = ?",
                (status, url, error, now_utc().isoformat() if status == "posted" else None, post_id),
            )

    def posted_since(self, platform: str, since: datetime, source: str | None = None) -> list[datetime]:
        q = "SELECT posted_at FROM posts WHERE platform = ? AND status = 'posted' AND posted_at >= ?"
        args: list = [platform, since.isoformat()]
        if source:
            q += " AND lower(source) = lower(?)"
            args.append(source)
        return [datetime.fromisoformat(r["posted_at"]) for r in self.db.execute(q, args)]

    def queued_posts(self, platform: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM posts WHERE status = 'queued'", []
        if platform:
            q += " AND platform = ?"
            args.append(platform)
        return [dict(r) for r in self.db.execute(q + " ORDER BY due_at", args)]

    def logged_posts(self, platform: str) -> list[ActivityItem]:
        """Replies you've posted through wingman, as activity (for platforms with no history API)."""
        rows = self.db.execute(
            "SELECT * FROM posts WHERE platform = ? AND status = 'posted' ORDER BY posted_at", (platform,))
        return [ActivityItem(platform=r["platform"], id=str(r["id"]), source=r["source"], body=r["text"], score=0,
                             created_at=datetime.fromisoformat(r["posted_at"]), permalink=r["url"] or "")
                for r in rows]

    def wingman_post_urls(self) -> set[str]:
        return {r["url"] for r in self.db.execute("SELECT url FROM posts WHERE status = 'posted' AND url IS NOT NULL")}

    # ratings -------------------------------------------------------------
    def add_rating(self, r: Rating) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO ratings (platform, thread_key, source, model, rating, outcome, similarity, "
                "regenerations, issues, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r.platform, r.thread_key, r.source, r.model, r.rating, r.outcome, r.similarity, r.regenerations,
                 json.dumps(r.issues), r.at or now_utc().isoformat()),
            )

    def ratings(self, platform: str | None = None) -> list[Rating]:
        q, args = "SELECT * FROM ratings", []
        if platform:
            q += " WHERE platform = ?"
            args.append(platform)
        return [Rating(platform=r["platform"], thread_key=r["thread_key"], source=r["source"], model=r["model"],
                       rating=r["rating"], outcome=r["outcome"], similarity=r["similarity"],
                       regenerations=r["regenerations"], issues=json.loads(r["issues"]), at=r["at"])
                for r in self.db.execute(q + " ORDER BY at", args)]

    # activity ------------------------------------------------------------
    def save_activity(self, items: list[ActivityItem]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO activity (platform, id, source, body, score, created_at, permalink) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(i.platform, i.id, i.source, i.body, i.score, i.created_at.isoformat(), i.permalink) for i in items],
            )

    def activity(self, platform: str) -> list[ActivityItem]:
        rows = self.db.execute("SELECT * FROM activity WHERE platform = ?", (platform,))
        return [ActivityItem(platform=r["platform"], id=r["id"], source=r["source"], body=r["body"], score=r["score"],
                             created_at=datetime.fromisoformat(r["created_at"]), permalink=r["permalink"] or "")
                for r in rows]
