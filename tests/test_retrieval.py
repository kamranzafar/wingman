# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

from datetime import date

from wingman.core.models import Document
from wingman.core.retrieval import Chunk, Index, Weighting, chunk_text, tokenize

TODAY = date(2026, 11, 18)


def test_tokenize_keeps_technical_terms_and_drops_stopwords():
    assert tokenize("How do I tune snapshot.mode in Debezium and C++?") == ["tune", "snapshot.mode", "debezium", "c++"]


def test_chunk_text_packs_paragraphs_and_splits_long_ones():
    paras = ["A" * 400, "B" * 400, "C" * 400]
    chunks = chunk_text("\n\n".join(paras), max_chars=1000)
    assert len(chunks) == 2 and chunks[0] == "A" * 400 + "\n\n" + "B" * 400
    long = chunk_text("x" * 2500, max_chars=1000, overlap=150)
    assert all(len(c) <= 1000 for c in long) and len(long) == 3


def test_chunk_text_drops_tiny_fragments():
    assert chunk_text("short", min_chars=120) == []


def test_weighting_prefers_recent_and_projects_but_has_a_floor():
    w = Weighting(half_life_years=5, floor=0.6, project_boost=1.2)
    new_blog = Chunk("b", "", "blog", "t", "2026-11-01")
    old_blog = Chunk("b", "", "blog", "t", "2011-01-01")
    repo = Chunk("r", "", "github", "t", "2026-11-01")
    undated = Chunk("p", "", "page", "t", "")
    assert w.weight(new_blog, TODAY) > 0.99
    assert w.weight(old_blog, TODAY) == 0.6  # 15 years old -> clamped at the floor
    assert w.weight(repo, TODAY) > w.weight(new_blog, TODAY)
    assert w.weight(undated, TODAY) == 1.0


def _index(docs):
    return Index.build([Document(title=t, text=x * 3, kind=k, date=d) for t, x, k, d in docs], embedder=None)


def test_precise_old_match_beats_vague_new_one():
    idx = _index([
        ("Tomcat clustering", "Session replication across a Tomcat cluster with DeltaManager. ", "blog", "2013-08-03"),
        ("Cloudkit", "One Java API for AWS, Azure and Google Cloud storage and queues. ", "project", "2026-11-01"),
    ])
    assert idx.search("How do I set up Tomcat session replication?", k=1)[0].source == "Tomcat clustering"


def test_project_wins_a_tie_on_relevance():
    text = "Feature toggles at a seam with one router class per feature, no if statements. "
    idx = _index([("Old post", text, "blog", "2016-01-01"), ("togglekit", text, "github", "2026-10-01")])
    assert idx.search("feature toggles router", k=1)[0].source == "togglekit"


def test_search_limits_chunks_per_source_and_skips_unrelated():
    idx = Index([Chunk("A", "", "blog", f"kafka connect {i}") for i in range(5)]
                + [Chunk("B", "", "blog", "raspberry pi adhan player")])
    hits = idx.search("kafka connect", k=4, max_per_source=2)
    assert [h.source for h in hits] == ["A", "A"]  # B has no overlap, so keyword-only search drops it


def test_save_and_load_round_trip(tmp_path):
    idx = _index([("Post", "Change data capture with Debezium and Kafka. ", "blog", "2025-07-03")])
    idx.save(tmp_path / "index.json")
    loaded = Index.load(tmp_path / "index.json")
    assert loaded.chunks == idx.chunks and loaded.mode == "keyword (BM25)"
    assert Index.load(tmp_path / "missing.json") is None
