"""Local hybrid retrieval over your own writing: BM25 always, plus Ollama embeddings when available.

Nothing here calls Claude, so searching is free. The index is a JSON file in data/.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from wingman.core import http
from wingman.core.models import Document

log = logging.getLogger(__name__)

DEFAULT_OLLAMA = "http://localhost:11434"


def ollama_url() -> str:
    """$OLLAMA_HOST (the variable Ollama itself uses, e.g. "gpu-box:11434"), else localhost."""
    host = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")
    if not host or host.startswith("0.0.0.0"):  # 0.0.0.0 is a bind address, not one to connect to
        return DEFAULT_OLLAMA
    return host if "://" in host else f"http://{host}"
EMBED_BATCH = 32
TOKEN = re.compile(r"[a-z0-9][a-z0-9_.+#-]*[a-z0-9+#]|[a-z0-9]")
STOP = frozenset("""
a an and are as at be but by can do for from has have how i if in into is it its just like me my no not
of on or our so than that the their them then there these they this to too was we were what when which
who why will with you your
""".split())
BM25_K1, BM25_B = 1.5, 0.75
FUSION_DEPTH = 50  # how many results from each ranking take part in fusion
PROJECT_KINDS = {"github", "project"}


@dataclass
class Chunk:
    source: str  # document title
    url: str
    kind: str
    text: str
    date: str = ""  # ISO date the source was written/last updated ("" = unknown / evergreen)


@dataclass(frozen=True)
class Weighting:
    """Prefer recent writing and your own projects, as a tie-breaker between similarly relevant passages."""
    half_life_years: float = 5.0  # a post this old counts half as much...
    floor: float = 0.6            # ...but never less than this
    project_boost: float = 1.2    # GitHub repos and project sites
    rrf_k: int = 10               # lower = relevance rank matters more relative to the weights

    def weight(self, chunk: Chunk, today: date) -> float:
        w = self.project_boost if chunk.kind in PROJECT_KINDS else 1.0
        if chunk.date:
            try:
                age_years = max((today - date.fromisoformat(chunk.date[:10])).days, 0) / 365.25
                w *= max(0.5 ** (age_years / self.half_life_years), self.floor)
            except ValueError:
                pass
        return w


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP]


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 150, min_chars: int = 120) -> list[str]:
    """Split on paragraphs, packing them into ~max_chars chunks (long paragraphs are split)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
    chunks, current = [], ""
    for p in paras:
        while len(p) > max_chars:
            chunks.append(p[:max_chars])
            p = p[max_chars - overlap:]
        if current and len(current) + len(p) + 2 > max_chars:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{p}" if current else p
    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) >= min_chars]


# ---------------------------------------------------------------- embeddings (optional)

class OllamaEmbedder:
    """Local embeddings via Ollama. Methods return None when Ollama isn't usable (not running, model not pulled)."""

    def __init__(self, model: str, url: str | None = None):
        self.model = model
        self.url = url or ollama_url()

    def _embed(self, texts: list[str], prefix: str) -> list[list[float]] | None:
        try:
            out: list[list[float]] = []
            for i in range(0, len(texts), EMBED_BATCH):
                batch = [prefix + t for t in texts[i:i + EMBED_BATCH]]
                out += http.post_json(f"{self.url}/api/embed", {"model": self.model, "input": batch})["embeddings"]
            return out
        except Exception as e:  # noqa: BLE001 - not running, model not pulled, etc.
            log.debug("Ollama embedding failed: %s", e)
            return None

    # nomic-embed-text expects task prefixes; other models ignore them harmlessly.
    def documents(self, texts: list[str]) -> list[list[float]] | None:
        return self._embed(texts, "search_document: ")

    def query(self, text: str) -> list[float] | None:
        result = self._embed([text[:2000]], "search_query: ")
        return result[0] if result else None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- index

class Index:
    def __init__(self, chunks: list[Chunk], vectors: list[list[float]] | None = None,
                 embedder: OllamaEmbedder | None = None, weighting: Weighting = Weighting()):
        self.chunks = chunks
        self.vectors = vectors if embedder else None
        self.embedder = embedder if vectors else None
        self.weighting = weighting
        today = date.today()
        self._weights = [weighting.weight(c, today) for c in chunks]
        self._docs = [tokenize(f"{c.source} {c.text}") for c in chunks]
        self._tfs = [Counter(d) for d in self._docs]
        self._df = Counter(t for d in self._docs for t in set(d))
        self._avgdl = sum(map(len, self._docs)) / max(len(self._docs), 1)

    @property
    def mode(self) -> str:
        return f"hybrid (BM25 + {self.embedder.model})" if self.embedder else "keyword (BM25)"

    def _bm25(self, query: list[str]) -> list[float]:
        n, terms = len(self._docs), set(query)
        idf = {t: math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5)) for t in terms}
        scores = []
        for doc, tf in zip(self._docs, self._tfs, strict=True):
            norm = BM25_K1 * (1 - BM25_B + BM25_B * len(doc) / self._avgdl)
            scores.append(sum(idf[t] * tf[t] * (BM25_K1 + 1) / (tf[t] + norm) for t in terms if t in tf))
        return scores

    def search(self, query: str, k: int = 4, max_per_source: int = 2) -> list[Chunk]:
        """Reciprocal-rank fusion of BM25 and (if indexed) embedding similarity, then weighting."""
        if not self.chunks:
            return []
        bm25 = self._bm25(tokenize(query))
        rankings = [sorted(range(len(bm25)), key=lambda i: -bm25[i])]
        if self.embedder and self.vectors:
            qv = self.embedder.query(query)
            if qv:
                sims = [_cosine(qv, v) for v in self.vectors]
                rankings.append(sorted(range(len(sims)), key=lambda i: -sims[i]))

        fused: Counter[int] = Counter()
        for ranking in rankings:
            for rank, i in enumerate(ranking[:FUSION_DEPTH]):
                fused[i] += 1 / (self.weighting.rrf_k + rank)
        if len(rankings) == 1:  # keyword-only: drop chunks with no term overlap at all
            fused = Counter({i: s for i, s in fused.items() if bm25[i] > 0})
        fused = Counter({i: s * self._weights[i] for i, s in fused.items()})

        results: list[Chunk] = []
        per_source: Counter[str] = Counter()
        for i, _ in fused.most_common():
            c = self.chunks[i]
            if per_source[c.source] < max_per_source:
                results.append(c)
                per_source[c.source] += 1
            if len(results) >= k:
                break
        return results

    # ------------------------------------------------------------ persistence
    def save(self, path: Path) -> None:
        path.write_text(json.dumps({
            "embed_model": self.embedder.model if self.embedder else None,
            "chunks": [asdict(c) for c in self.chunks],
            "vectors": self.vectors,
        }))

    @classmethod
    def load(cls, path: Path, weighting: Weighting = Weighting()) -> Index | None:
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        embedder = OllamaEmbedder(d["embed_model"]) if d.get("embed_model") else None
        return cls([Chunk(**c) for c in d["chunks"]], d.get("vectors"), embedder, weighting)

    @classmethod
    def build(cls, documents: list[Document], embedder: OllamaEmbedder | None,
              weighting: Weighting = Weighting()) -> Index:
        chunks = [Chunk(source=d.title, url=d.url, kind=d.kind, text=t, date=d.date)
                  for d in documents for t in chunk_text(d.text)]
        vectors = embedder.documents([f"{c.source}\n{c.text}" for c in chunks]) if embedder and chunks else None
        return cls(chunks, vectors, embedder, weighting)
