"""PaperLib V4 local-first academic intelligence primitives.

No network calls and no model downloads happen in this module. It provides:
- persistent block-level corpus index in SQLite;
- deterministic BM25 retrieval;
- optional local dense embeddings through FlagEmbedding (BGE-M3);
- reciprocal-rank fusion;
- conservative rule-based academic structure candidates.

Dense embeddings are opt-in via PAPERLIB_BGE_M3_PATH pointing to an already
downloaded local model directory. The default path is lexical-only.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..db import db, row, rows
from ..util import dumps, loads, now

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+.-]*|\d+(?:\.\d+)?|[\u3400-\u9fff]{2,}")
ACADEMIC_PATTERNS = [
    ("definition", re.compile(r"\b(definition|defined as|we define|is defined as)\b|定义为|定义如下", re.I)),
    ("assumption", re.compile(r"\b(assumption|assume that|under the assumption)\b|假设", re.I)),
    ("theorem", re.compile(r"\b(theorem|proposition|lemma|corollary)\b|定理|命题|引理|推论", re.I)),
    ("condition", re.compile(r"\b(necessary condition|sufficient condition|if and only if|iff|necessary and sufficient)\b|必要条件|充分条件|当且仅当", re.I)),
    ("proof", re.compile(r"\b(proof|we prove|it follows that)\b|证明", re.I)),
    ("limitation", re.compile(r"\b(limitation|however,|we leave .* for future work)\b|局限|未来工作", re.I)),
]
DEFAULT_TOP_K = 8


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for token in TOKEN_RE.findall((text or "").lower()):
        if re.fullmatch(r"[\u3400-\u9fff]{2,}", token):
            tokens.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            tokens.append(token)
    return tokens


def ensure_tables() -> None:
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_intelligence_chunks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id TEXT NOT NULL,
                block_id INTEGER NOT NULL,
                section_title TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                terms_json TEXT NOT NULL DEFAULT '[]',
                embedding_json TEXT NOT NULL DEFAULT '',
                embedding_model TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(paper_id, block_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pi_chunks_paper ON paper_intelligence_chunks(paper_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_intelligence_meta(
                paper_id TEXT PRIMARY KEY,
                indexed_blocks INTEGER NOT NULL DEFAULT 0,
                indexed_chars INTEGER NOT NULL DEFAULT 0,
                embedding_model TEXT NOT NULL DEFAULT '',
                index_status TEXT NOT NULL DEFAULT 'lexical',
                updated_at TEXT NOT NULL DEFAULT ''
            )
        """)


def _section_titles(paper_id: str) -> dict[int, str]:
    section_rows = rows(
        "SELECT title_en,title,block_start FROM sections WHERE paper_id=? ORDER BY order_idx",
        (paper_id,),
    )
    starts = [(int(s["block_start"]), str(s.get("title_en") or s.get("title") or ""))
              for s in section_rows if s.get("block_start") is not None]
    result: dict[int, str] = {}
    current = "Unsectioned"
    for block in rows("SELECT id FROM blocks WHERE paper_id=? ORDER BY order_idx", (paper_id,)):
        bid = int(block["id"])
        for start, title in starts:
            if start == bid:
                current = title
                break
        result[bid] = current
    return result


def _encode_batch(model, texts: list[str]) -> list[list[float]]:
    encoded = model.encode(texts)
    # FlagEmbedding BGEM3FlagModel returns a dict with dense_vecs.
    if isinstance(encoded, dict):
        vectors = encoded.get("dense_vecs")
        if vectors is None:
            vectors = encoded.get("sentence_embeddings")
    else:
        vectors = encoded
    if hasattr(vectors, "tolist"):
        vectors = vectors.tolist()
    return [[float(x) for x in vector] for vector in vectors]


def _encode_one(model, text: str) -> list[float]:
    return _encode_batch(model, [text])[0]


def index_paper(paper_id: str, include_embeddings: bool = False) -> dict[str, Any]:
    """Index parsed English blocks. Re-indexing replaces only this paper's search index."""
    ensure_tables()
    if not row("SELECT id FROM papers WHERE id=?", (paper_id,)):
        raise ValueError("论文不存在")
    section_map = _section_titles(paper_id)
    blocks = rows(
        "SELECT id,en,type FROM blocks WHERE paper_id=? AND type IN ('text','heading','list','table') ORDER BY order_idx",
        (paper_id,),
    )
    eligible = []
    for block in blocks:
        text = str(block.get("en") or "").strip()
        if len(text) >= 20:
            eligible.append((block, text, tokenize(text)))
    model = _load_embedding_model() if include_embeddings else None
    if include_embeddings and model is None:
        raise RuntimeError("尚未配置 PAPERLIB_BGE_M3_PATH；请先设置已下载的本地 BGE-M3 模型目录")
    vectors: dict[int, list[float]] = {}
    if model is not None:
        # Batch encoding is much faster than one model invocation per paragraph.
        for offset in range(0, len(eligible), 16):
            batch = eligible[offset:offset + 16]
            encoded = _encode_batch(model, [item[1] for item in batch])
            for item, vector in zip(batch, encoded):
                vectors[int(item[0]["id"])] = vector
    indexed, chars = 0, 0
    seen_ids = set()
    with db() as conn:
        for block, text, terms in eligible:
            block_id = int(block["id"])
            seen_ids.add(block_id)
            vector = vectors.get(block_id, [])
            model_name = os.environ.get("PAPERLIB_BGE_M3_PATH", "") if vector else ""
            conn.execute("""
                INSERT INTO paper_intelligence_chunks
                    (paper_id,block_id,section_title,text,terms_json,embedding_json,embedding_model,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(paper_id,block_id) DO UPDATE SET
                    section_title=excluded.section_title,text=excluded.text,
                    terms_json=excluded.terms_json,embedding_json=excluded.embedding_json,
                    embedding_model=excluded.embedding_model,updated_at=excluded.updated_at
            """, (paper_id, block_id, section_map.get(block_id, ""), text, dumps(terms),
                  dumps(vector) if vector else "", model_name, now()))
            indexed += 1
            chars += len(text)
        if seen_ids:
            placeholders = ",".join("?" for _ in seen_ids)
            conn.execute(
                f"DELETE FROM paper_intelligence_chunks WHERE paper_id=? AND block_id NOT IN ({placeholders})",
                (paper_id, *sorted(seen_ids)),
            )
        else:
            conn.execute("DELETE FROM paper_intelligence_chunks WHERE paper_id=?", (paper_id,))
        conn.execute("""
            INSERT INTO paper_intelligence_meta(paper_id,indexed_blocks,indexed_chars,embedding_model,index_status,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(paper_id) DO UPDATE SET indexed_blocks=excluded.indexed_blocks,
                indexed_chars=excluded.indexed_chars,embedding_model=excluded.embedding_model,
                index_status=excluded.index_status,updated_at=excluded.updated_at
        """, (paper_id, indexed, chars, os.environ.get("PAPERLIB_BGE_M3_PATH", "") if model else "",
              "hybrid" if model else "lexical", now()))
    return {"paper_id": paper_id, "indexed_blocks": indexed, "indexed_chars": chars,
            "mode": "hybrid" if model else "lexical",
            "embedding_model": os.environ.get("PAPERLIB_BGE_M3_PATH", "") if model else ""}


@lru_cache(maxsize=1)
def _load_embedding_model():
    model_path = os.environ.get("PAPERLIB_BGE_M3_PATH", "").strip()
    if not model_path:
        return None
    if not Path(model_path).exists():
        raise RuntimeError("PAPERLIB_BGE_M3_PATH 必须指向已下载到本机的 BGE-M3 模型目录；不会自动联网下载")
    try:
        from FlagEmbedding import BGEM3FlagModel
    except ImportError as exc:
        raise RuntimeError("启用 BGE-M3 前请安装可选依赖：pip install -r backend/requirements-intelligence.txt") from exc
    return BGEM3FlagModel(model_path, use_fp16=False)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _dense_query(query: str) -> list[float] | None:
    model = _load_embedding_model()
    if model is None:
        return None
    return _encode_one(model, query)


def search_blocks(query: str, paper_id: str | None = None, top_k: int = DEFAULT_TOP_K,
                  dense: bool = True) -> dict[str, Any]:
    """BM25 + optional dense retrieval with reciprocal-rank fusion."""
    ensure_tables()
    sql = "SELECT * FROM paper_intelligence_chunks"
    args: tuple = ()
    if paper_id:
        sql += " WHERE paper_id=?"
        args = (paper_id,)
    docs = rows(sql, args)
    if not docs:
        return {"query": query, "mode": "empty", "results": []}
    qterms = tokenize(query)
    n = len(docs)
    doc_terms = [loads(d["terms_json"], []) for d in docs]
    avgdl = sum(len(t) for t in doc_terms) / max(1, n)
    df = Counter(term for terms in doc_terms for term in set(terms))
    lexical = []
    for doc, terms in zip(docs, doc_terms):
        tf = Counter(terms)
        score = 0.0
        for term in qterms:
            freq = tf[term]
            if not freq:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = freq + 1.2 * (1 - 0.75 + 0.75 * len(terms) / max(1.0, avgdl))
            score += idf * freq * 2.2 / denom
        if score:
            lexical.append((score, doc))
    lexical.sort(key=lambda x: x[0], reverse=True)
    configured_model = os.environ.get("PAPERLIB_BGE_M3_PATH", "")
    dense_docs = [doc for doc in docs
                  if doc.get("embedding_json") and doc.get("embedding_model") == configured_model]
    query_vector = _dense_query(query) if dense and dense_docs and configured_model else None
    fused: dict[int, dict[str, Any]] = {}
    for rank, (score, doc) in enumerate(lexical, 1):
        bid = int(doc["block_id"])
        fused[bid] = {"doc": doc, "lexical_score": score, "dense_score": 0.0, "rrf": 1 / (60 + rank)}
    for rank, (_, doc) in enumerate(sorted(
        [(_cosine(query_vector, loads(d.get("embedding_json"), [])), d) for d in dense_docs
         if query_vector], key=lambda x: x[0], reverse=True), 1):
        bid = int(doc["block_id"])
        item = fused.setdefault(bid, {"doc": doc, "lexical_score": 0.0, "dense_score": 0.0, "rrf": 0.0})
        item["dense_score"] = _cosine(query_vector, loads(doc.get("embedding_json"), []))
        item["rrf"] += 1 / (60 + rank)
    ranked = sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)[:max(1, min(int(top_k), 50))]
    return {"query": query, "mode": "hybrid" if query_vector else "lexical",
            "results": [{"paper_id": x["doc"]["paper_id"], "block_id": x["doc"]["block_id"],
                         "section_title": x["doc"]["section_title"], "text": x["doc"]["text"],
                         "lexical_score": round(x["lexical_score"], 5),
                         "dense_score": round(x["dense_score"], 5), "rrf_score": round(x["rrf"], 6)}
                        for x in ranked]}


@lru_cache(maxsize=1)
def _load_gliner():
    model_path = os.environ.get("PAPERLIB_GLINER_PATH", "").strip()
    if not model_path:
        return None
    if not Path(model_path).exists():
        raise RuntimeError("PAPERLIB_GLINER_PATH 必须指向已下载到本机的 GLiNER 模型目录；不会自动联网下载")
    try:
        from gliner import GLiNER
    except ImportError as exc:
        raise RuntimeError("启用 GLiNER 前请安装可选依赖：pip install -r backend/requirements-intelligence.txt") from exc
    return GLiNER.from_pretrained(model_path)


def extract_structure_candidates(paper_id: str) -> dict[str, Any]:
    """Conservative rule-based candidate discovery; all results point to source blocks."""
    blocks = rows(
        "SELECT id,en FROM blocks WHERE paper_id=? AND type IN ('text','heading','list','table') ORDER BY order_idx",
        (paper_id,),
    )
    candidates = []
    for block in blocks:
        text = str(block.get("en") or "")
        for kind, pattern in ACADEMIC_PATTERNS:
            match = pattern.search(text)
            if match:
                excerpt = text[max(0, match.start() - 100):min(len(text), match.end() + 240)].strip()
                candidates.append({"kind": kind, "block_id": int(block["id"]),
                                   "matched_term": match.group(0), "excerpt": excerpt,
                                   "source": "rule", "score": 1.0})
    # Optional local GLiNER zero-shot entity proposals. It never downloads weights.
    model_error = ""
    try:
        model = _load_gliner()
    except Exception as exc:
        model = None
        model_error = f"{type(exc).__name__}: {str(exc)[:180]}"
    if model is not None:
        labels = ["definition", "assumption", "necessary condition", "sufficient condition",
                  "theorem", "proposition", "lemma", "corollary", "proof", "method",
                  "dataset", "evaluation metric", "limitation"]
        for block in blocks:
            text = str(block.get("en") or "")
            if len(text) < 30:
                continue
            for entity in model.predict_entities(text, labels, threshold=0.45):
                start, end = int(entity.get("start", 0)), int(entity.get("end", 0))
                if start < 0 or end <= start or end > len(text):
                    continue
                quote = text[start:end]
                candidates.append({"kind": str(entity.get("label") or "academic_entity").lower(),
                                   "block_id": int(block["id"]), "matched_term": quote,
                                   "excerpt": text[max(0, start - 100):min(len(text), end + 240)].strip(),
                                   "source": "gliner", "score": round(float(entity.get("score") or 0), 4)})
    # Stable de-duplication by source span and semantic label.
    unique, seen = [], set()
    for item in candidates:
        key = (item["block_id"], item["kind"], item["matched_term"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return {"paper_id": paper_id, "candidate_count": len(unique), "candidates": unique,
            "warnings": [model_error] if model_error else []}


def index_status(paper_id: str) -> dict[str, Any] | None:
    ensure_tables()
    return row("SELECT * FROM paper_intelligence_meta WHERE paper_id=?", (paper_id,))
