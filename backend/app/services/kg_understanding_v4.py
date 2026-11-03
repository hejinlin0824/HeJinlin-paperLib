"""V4 orchestration: local-first indexing, bounded LLM reasoning, recoverable drafts."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..db import db, row
from ..util import now
from . import kg_understanding as legacy
from .paper_understanding_common import (
    CHUNK_SYSTEM, SYNTHESIS_SYSTEM, _chunk_prompt, _load_sections, _make_chunks,
    _validate_logic, compact_chunk_notes,
)
from . import paper_intelligence_v4 as intelligence
from .translate import chat, engine_info, engine_ready
from .structured_output import (
    batch_chunk_schema, chunk_analysis_schema, parse_and_validate,
    response_format as make_response_format, synthesis_schema, verification_schema,
)

logger = logging.getLogger(__name__)
VERSION = "4.0"


def create_draft(paper_id: str) -> dict:
    draft = legacy.create_draft(paper_id)
    with db() as conn:
        conn.execute(
            "UPDATE kg_understanding_drafts SET version=?,updated_at=? WHERE id=?",
            (VERSION, now(), draft["id"]),
        )
    draft["version"] = VERSION
    return draft


def list_drafts(paper_id: str | None = None) -> list[dict]:
    return [item for item in legacy.list_drafts(paper_id) if item.get("version") == VERSION]


def get_draft(draft_id: str) -> dict | None:
    item = legacy.get_draft(draft_id)
    return item if item and item.get("version") == VERSION else None


def update_review(draft_id: str, payload: dict, status: str | None = None) -> dict:
    current = row("SELECT version FROM kg_understanding_drafts WHERE id=?", (draft_id,))
    if not current or current.get("version") != VERSION:
        raise ValueError("V4 理解档案不存在或版本不匹配")
    return legacy.update_review(draft_id, payload, status)


def _fallback_payload(title: str, section_map: list[dict], candidates: list[dict]) -> dict[str, Any]:
    contributions = []
    for i, item in enumerate(candidates[:40], 1):
        contributions.append({
            "id": f"rule_{i}", "title": f"{item['kind'].title()}: {item['matched_term']}",
            "description": item["excerpt"][:500], "kind": item["kind"],
            "importance": "supporting", "knowledge_role": "rule_candidate",
            "parent_id": None, "evidence": [{"block_id": item["block_id"], "quote": item["excerpt"][:240]}],
            "origin": "rule_candidate_not_model_verified",
        })
    return {
        "version": VERSION, "paper_title": title, "paper_type": "other",
        "problem": "", "thesis": "", "contributions": contributions,
        "theoretical_framework": [], "method_architecture": [], "key_findings": [],
        "limitations": [], "uncertainties": ["LLM 综合阶段未完成；当前内容仅为规则识别候选，需人工审阅。"],
        "relations": [], "theory_logic": [], "argument_map": [], "section_map": section_map,
    }


async def _chat_v4(info: dict, messages: list, max_tokens: int,
                   schema_name: str | None = None, schema: dict | None = None):
    """Prefer strict JSON Schema, degrade only for explicit provider capability errors."""
    kwargs = {"max_tokens": max_tokens, "timeout_seconds": 90, "retries": 2}
    if schema is None or not schema_name:
        return await chat(info, messages, **kwargs)
    constrained = make_response_format(schema_name, schema)
    try:
        return await chat(info, messages, response_format=constrained, **kwargs)
    except RuntimeError as exc:
        detail = str(exc).lower()
        unsupported = any(token in detail for token in (
            "response_format", "json_schema", "unsupported parameter", "not support",
            "unknown field", "unrecognized request argument",
        ))
        if not unsupported:
            raise
        logger.warning("Provider rejected strict JSON Schema; trying JSON mode: %s", type(exc).__name__)
    try:
        return await chat(info, messages, response_format={"type": "json_object"}, **kwargs)
    except RuntimeError as exc:
        detail = str(exc).lower()
        if not any(token in detail for token in ("response_format", "json_object", "unsupported parameter", "not support")):
            raise
        logger.warning("Provider rejected JSON mode; falling back to prompt-only JSON: %s", type(exc).__name__)
        return await chat(info, messages, **kwargs)


async def probe_structured_output() -> dict:
    """Explicit, user-triggered capability probe; never runs during startup or CI."""
    info = engine_info()
    ready, reason = engine_ready(info)
    if not ready:
        return {"status": "not_configured", "provider": info.get("provider"),
                "model": info.get("model"), "reason": reason}
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    try:
        raw = await chat(info, [
            {"role": "system", "content": "Return the requested JSON object."},
            {"role": "user", "content": "Return exactly {\"ok\": true}."},
        ], max_tokens=80, timeout_seconds=30, retries=1,
           response_format=make_response_format("paperlib_capability_probe", schema))
        value = parse_and_validate(raw, schema)
        return {"status": "supported", "mode": "json_schema", "provider": info.get("provider"),
                "model": info.get("model"), "valid_response": value}
    except Exception as exc:
        return {"status": "unsupported_or_failed", "mode": "json_schema",
                "provider": info.get("provider"), "model": info.get("model"),
                "error_type": type(exc).__name__, "reason": str(exc)[:500]}


def _compact_notes(notes: list[dict], max_chars: int = 48000) -> tuple[str, dict]:
    """Bounded local compaction; preserves a compact record for every analyzed chunk."""
    return compact_chunk_notes(notes, max_chars=max_chars)


CHUNK_BATCH_MAX_CHARS = 11800
CHUNK_BATCH_MAX_ITEMS = 2


def _build_chunk_batches(chunks: list[dict]) -> list[list[int]]:
    """Pack adjacent short chunks; each retains its own section and block IDs."""
    batches: list[list[int]] = []
    current: list[int] = []
    current_chars = 0
    for index, chunk in enumerate(chunks):
        prompt_chars = len(_chunk_prompt("", chunk))
        if current and (len(current) >= CHUNK_BATCH_MAX_ITEMS
                        or current_chars + prompt_chars > CHUNK_BATCH_MAX_CHARS):
            batches.append(current)
            current, current_chars = [], 0
        current.append(index)
        current_chars += prompt_chars
    if current:
        batches.append(current)
    return batches


def _batch_chunk_prompt(title: str, batch_id: int, indexes: list[int], chunks: list[dict]) -> str:
    parts = [f"论文：{title}", f"本请求包含 {len(indexes)} 个独立片段。必须逐个分析，不能跨片段合并证据。"]
    for index in indexes:
        chunk = chunks[index]
        parts.append(f"<chunk id=\"{index}\">\n{_chunk_prompt(title, chunk)}\n</chunk>")
    parts.append(
        '只输出 JSON：{"chunks":[{"chunk_id":0,"section_role":"...","summary":"...",'
        '"claims":[],"argument_steps":[],"candidate_contributions":[],"open_questions":[]}]}。'
        "chunks 数组必须对输入的每个 chunk id 恰好返回一项；不得遗漏、重复或改写 block_id。"
    )
    return "\n\n".join(parts)

def _normalise_batch_notes(raw_response: str, expected: list[int], chunks: list[dict]) -> dict[int, dict]:
    parsed = legacy._json(raw_response)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("chunks"), list):
        return {}
    accepted: dict[int, dict] = {}
    expected_set = set(expected)
    for item in parsed["chunks"]:
        if not isinstance(item, dict):
            continue
        try:
            chunk_id = int(item.get("chunk_id"))
        except (TypeError, ValueError):
            continue
        if chunk_id not in expected_set or chunk_id in accepted or not item.get("summary"):
            continue
        accepted[chunk_id] = {
            "title": chunks[chunk_id]["section"],
            "chunk_index": chunks[chunk_id]["chunk_index"],
            "section_index": chunks[chunk_id]["section_index"],
            **{key: value for key, value in item.items() if key != "chunk_id"},
        }
    return accepted


async def run_understanding(draft_id: str) -> None:
    legacy.ensure_tables()
    draft = row("SELECT * FROM kg_understanding_drafts WHERE id=?", (draft_id,))
    if not draft:
        return
    info = engine_info()
    model_name = f"{info.get('provider') or 'unknown'}/{info.get('model') or 'unknown'}"
    warnings: list[str] = []
    run_started = time.perf_counter()
    payload: dict[str, Any] = {"version": VERSION, "section_map": [], "coverage": {},
                               "pipeline": {"llm_operations": 0, "retrieval": "not_indexed", "timings_seconds": {}}}
    try:
        paper = row("SELECT id,title,title_zh FROM papers WHERE id=?", (draft["paper_id"],))
        if not paper:
            raise ValueError("论文记录不存在")
        title = paper.get("title_zh") or paper.get("title") or "未命名论文"
        # Indexing is deterministic and local. Optional dense embeddings are opt-in.
        try:
            index_result = intelligence.index_paper(draft["paper_id"], include_embeddings=False)
            payload["pipeline"]["retrieval"] = index_result["mode"]
            payload["pipeline"]["indexed_blocks"] = index_result["indexed_blocks"]
        except Exception as exc:
            warnings.append(f"本地检索索引建立失败，继续论文理解：{type(exc).__name__}")
        try:
            candidate_result = intelligence.extract_structure_candidates(draft["paper_id"])
            candidates = candidate_result["candidates"]
            warnings.extend(candidate_result.get("warnings") or [])
        except Exception as exc:
            candidates = []
            warnings.append(f"规则候选提取不可用，继续主流程：{type(exc).__name__}")
        payload["pipeline"]["rule_candidates"] = len(candidates)
        sections, block_map = _load_sections(draft["paper_id"])
        chunks = _make_chunks(sections)
        if not chunks:
            raise ValueError("没有可分析的正文分块")
        total_chars = sum(len(x["text"]) for section in sections for x in section["blocks"])
        total_chunk_chars = sum(len(x["text"]) for chunk in chunks for x in chunk["segments"])
        notes: list[dict] = []
        failed: list[dict] = []
        processed_chars = 0
        payload.update({"paper_title": title, "progress_stage": "chunk_analysis"})
        legacy._persist_progress(draft_id, model_name, payload, warnings)
        ready, reason = engine_ready(info)
        if not ready:
            warnings.append(f"模型未就绪：{reason}；保留本地规则候选与章节结构。")
        else:
            max_concurrency = max(1, min(3, int(info.get("concurrency") or 2)))
            semaphore = asyncio.Semaphore(max_concurrency)
            batches = _build_chunk_batches(chunks)
            payload["pipeline"]["chunk_batches"] = len(batches)
            payload["pipeline"]["chunk_batch_sizes"] = [len(batch) for batch in batches]
            payload["pipeline"]["llm_operations"] += sum(1 for batch in batches if len(batch) > 1)

            async def analyze_single(index: int):
                chunk = chunks[index]
                raw_response = await _chat_v4(info, [
                    {"role": "system", "content": CHUNK_SYSTEM},
                    {"role": "user", "content": _chunk_prompt(title, chunk)},
                ], max_tokens=2200, schema_name="paperlib_v4_chunk", schema=chunk_analysis_schema())
                result = parse_and_validate(raw_response, chunk_analysis_schema())
                if not result:
                    preview = " ".join(str(raw_response or "").split())[:180]
                    raise RuntimeError(
                        "模型未返回合法 JSON；"
                        f"response_chars={len(str(raw_response or ''))}; "
                        f"response_preview={preview!r}"
                    )
                return {"title": chunk["section"], "chunk_index": chunk["chunk_index"],
                        "section_index": chunk["section_index"], **result}

            async def analyze_batch(batch_number: int, indexes: list[int]):
                async with semaphore:
                    batch_notes: dict[int, dict] = {}
                    if len(indexes) > 1:
                        try:
                            raw = await _chat_v4(info, [
                                {"role": "system", "content": CHUNK_SYSTEM + "\n\n批量模式：每个 <chunk id> 是独立任务。"
                                 '严格返回 {"chunks":[{"chunk_id":整数,"section_role":...,"summary":...,'
                                 '"claims":...,"argument_steps":...,"candidate_contributions":...,"open_questions":...}]}。'},
                                {"role": "user", "content": _batch_chunk_prompt(title, batch_number, indexes, chunks)},
                            ], max_tokens=min(6600, 2200 * len(indexes)),
                                schema_name="paperlib_v4_chunk_batch", schema=batch_chunk_schema())
                            parse_and_validate(raw, batch_chunk_schema())
                            batch_notes = _normalise_batch_notes(raw, indexes, chunks)
                        except Exception as exc:
                            logger.warning("V4 chunk batch %s failed; falling back to single chunks: %s",
                                           batch_number, type(exc).__name__)
                    missing = [index for index in indexes if index not in batch_notes]
                    for index in missing:
                        try:
                            payload["pipeline"]["llm_operations"] += 1
                            batch_notes[index] = await analyze_single(index)
                        except Exception as exc:
                            failed.append({"section": chunks[index]["section"],
                                           "chunk_index": chunks[index]["chunk_index"],
                                           "error_type": type(exc).__name__, "message": str(exc)[:240]})
                    return batch_notes

            chunk_stage_started = time.perf_counter()
            tasks = [asyncio.create_task(analyze_batch(batch_number, indexes))
                     for batch_number, indexes in enumerate(batches)]
            for future in asyncio.as_completed(tasks):
                batch_notes = await future
                for index, note in batch_notes.items():
                    notes.append(note)
                    processed_chars += sum(len(segment["text"]) for segment in chunks[index]["segments"])
                notes.sort(key=lambda item: (item.get("section_index", 0), item.get("chunk_index", 0)))
                failed.sort(key=lambda item: (item["section"], item["chunk_index"]))
                payload["chunk_notes"] = notes
                payload["chunk_progress"] = {"completed": len(notes) + len(failed), "total": len(chunks)}
                payload["coverage"] = {
                    "section_count": len(sections), "chunk_count": len(chunks),
                    "completed_chunks": len(notes), "failed_chunks": len(failed), "not_attempted_chunks": 0,
                    "total_blocks": len(block_map), "total_chars": total_chars,
                    "processed_chars": processed_chars,
                    "chunked_chars": total_chunk_chars,
                    "input_coverage": round(processed_chars / max(1, total_chunk_chars), 4),
                    "complete": len(failed) == 0,
                }
                payload["failed_chunks"] = failed
                legacy._persist_progress(draft_id, model_name, payload, warnings)
            payload["pipeline"]["timings_seconds"]["chunk_analysis"] = round(time.perf_counter() - chunk_stage_started, 3)
        # Build section summaries from available chunk notes, including partial chapters.
        section_map = []
        for si, section in enumerate(sections):
            group = [n for n in notes if n.get("section_index") == si]
            section_map.append({
                "title": section["title"],
                "section_role": group[0].get("section_role", "other") if group else "other",
                "summary": " ".join(str(n.get("summary") or "") for n in group)[:1800],
                "claims": [claim for n in group for claim in (n.get("claims") or [])][:30],
                "argument_steps": [step for n in group for step in (n.get("argument_steps") or [])][:40],
                "coverage_status": "complete" if group and len(group) == sum(1 for c in chunks if c["section_index"] == si) else "partial" if group else "failed",
            })
        payload["section_map"] = section_map
        payload["progress_stage"] = "contribution_synthesis"
        legacy._persist_progress(draft_id, model_name, payload, warnings)
        if ready and notes:
            # No LLM summary-compression stage. Deterministically compact complete note objects.
            compact_notes, compact_stats = _compact_notes(notes)
            payload["pipeline"]["synthesis_input"] = compact_stats
            synthesis_started = time.perf_counter()
            try:
                payload["pipeline"]["llm_operations"] += 1
                synthesis_raw = await _chat_v4(info, [
                    {"role": "system", "content": SYNTHESIS_SYSTEM + "\\n所有数组对象必须严格包含 Schema 中定义的字段；证据字段统一使用逐字引用数组 [{block_id,quote}]，关系证据使用单个 {block_id,quote}。不得输出额外字段。"},
                    {"role": "user", "content": f"论文标题：{title}\n章节分块分析（证据均带 block_id；共 {compact_stats['chunks_included']} 个分块）：\n{compact_notes}"},
                ], max_tokens=7500, schema_name="paperlib_v4_synthesis", schema=synthesis_schema())
                result = parse_and_validate(synthesis_raw, synthesis_schema())
                if not result:
                    raise RuntimeError("论文综合阶段未返回合法 JSON")
                for key in ("contributions", "theoretical_framework", "method_architecture", "key_findings",
                            "limitations", "uncertainties", "relations", "theory_logic", "argument_map"):
                    result.setdefault(key, [])
                result["version"] = VERSION
                result["paper_title"] = title
                result["section_map"] = section_map
                result["chunk_notes"] = notes
                result["coverage"] = payload.get("coverage", {})
                result["pipeline"] = payload["pipeline"]
                payload = result
                payload["pipeline"].setdefault("timings_seconds", {})["synthesis"] = round(time.perf_counter() - synthesis_started, 3)
            except Exception as exc:
                payload["pipeline"].setdefault("timings_seconds", {})["synthesis"] = round(time.perf_counter() - synthesis_started, 3)
                warnings.append(f"全文综合失败，降级为规则候选与章节摘要：{type(exc).__name__}: {str(exc)[:200]}")
                payload.update(_fallback_payload(title, section_map, candidates))
        else:
            payload.update(_fallback_payload(title, section_map, candidates))
        # Keep V4 metadata and source coverage when falling back.
        payload["version"] = VERSION
        payload["paper_title"] = title
        payload["pipeline"] = payload.get("pipeline") or {"retrieval": "lexical", "llm_operations": 0}
        payload["section_map"] = payload.get("section_map") or section_map
        payload["coverage"] = payload.get("coverage") or {
            "section_count": len(sections), "chunk_count": len(chunks), "completed_chunks": len(notes),
            "failed_chunks": len(failed), "not_attempted_chunks": len(chunks) if not ready else 0,
            "total_blocks": len(block_map), "total_chars": total_chars,
            "processed_chars": 0, "chunked_chars": total_chunk_chars, "input_coverage": 0.0,
            "complete": False,
        }
        payload["failed_chunks"] = failed
        payload["progress_stage"] = "evidence_validation"
        warnings.extend(legacy._validate_evidence(payload, block_map))
        warnings.extend(legacy._normalise_contributions(payload))
        for node in payload.get("contributions", []):
            role = str(node.get("knowledge_role") or "").lower()
            if role not in {"formal_definition", "operational_criterion", "example_or_case",
                            "non_essential_property", "proof_or_argument", "background", "core_claim", "rule_candidate"}:
                node["knowledge_role"] = "core_claim" if node.get("importance") == "core" else "proof_or_argument"
        if payload.get("paper_type") not in {"theory", "algorithm", "system", "empirical", "survey", "other"}:
            payload["paper_type"] = "other"
        warnings.extend(legacy._normalise_relations(payload, block_map))
        warnings.extend(_validate_logic(payload, block_map))
        # Verify all candidate edges in one bounded request, not one request per edge.
        node_map = legacy.nodes_by_id(payload)
        edges = []
        for index, relation in enumerate(payload.get("relations", [])[:12], 1):
            relation["id"] = str(relation.get("id") or f"rel_{index}")
            edges.append({"id": relation.get("id") or f"rel_{len(edges)+1}",
                          "relation_type": relation.get("relation_type"),
                          "source": node_map.get(relation.get("source_id")),
                          "target": node_map.get(relation.get("target_id")),
                          "evidence": relation.get("evidence")})
        for logic in payload.get("theory_logic", [])[:12]:
            edges.append({"id": logic.get("id") or f"logic_{len(edges)+1}",
                          "relation_type": logic.get("relation_type"),
                          "sources": [node_map.get(x) for x in logic.get("source_ids", [])],
                          "target": node_map.get(logic.get("target_id")),
                          "evidence": logic.get("evidence")})
        if ready and edges:
            verification_started = time.perf_counter()
            try:
                payload["pipeline"]["llm_operations"] += 1
                verification_raw = await _chat_v4(info, [
                    {"role": "system", "content": "你是学术关系语义核验器。逐条判断证据是否蕴含关系。联合充分关系必须把所有前提作为整体检查。不得依赖常识补足证据。输出符合 Schema 的 verifications 数组。"},
                    {"role": "user", "content": json.dumps({"edges": edges}, ensure_ascii=False)},
                ], max_tokens=2200, schema_name="paperlib_v4_verifications", schema=verification_schema())
                verdicts = parse_and_validate(verification_raw, verification_schema())
                by_id = {str(x.get("id")): x for x in (verdicts or {}).get("verifications", []) if isinstance(x, dict)}
                for collection in (payload.get("relations", []), payload.get("theory_logic", [])):
                    for edge in collection:
                        verdict = by_id.get(str(edge.get("id") or ""))
                        if verdict and verdict.get("verdict") in {"entailed", "not_entailed", "uncertain"}:
                            edge["verification"] = verdict["verdict"]
                            edge["reason"] = str(verdict.get("reason") or "")[:500]
            except Exception as exc:
                warnings.append(f"批量关系核验失败，保留未核验候选：{type(exc).__name__}")
            finally:
                payload["pipeline"].setdefault("timings_seconds", {})["verification"] = round(time.perf_counter() - verification_started, 3)
        for relation in payload.get("relations", []):
            relation.setdefault("verification", "unverified")
        for relation in payload.get("theory_logic", []):
            relation.setdefault("verification", "unverified")
        payload.pop("progress_stage", None)
        if failed:
            warnings.append("部分分块未完成；其余结果已保留，档案为不完整草稿。")
        payload["pipeline"].setdefault("timings_seconds", {})["total"] = round(time.perf_counter() - run_started, 3)
        legacy._persist_progress(draft_id, model_name, payload, warnings, "draft")
    except Exception as exc:
        logger.exception("V4 paper understanding failed: draft=%s paper=%s", draft_id, draft.get("paper_id"))
        warnings.append(f"V4流程异常：{type(exc).__name__}: {str(exc)[:260]}")
        # A recoverable draft is preferable to destroying successful partial work.
        payload["version"] = VERSION
        payload["progress_stage"] = "partial_recovery"
        legacy._persist_progress(draft_id, model_name, payload, warnings, "draft")


def index_status(paper_id: str) -> dict | None:
    return intelligence.index_status(paper_id)


def index_paper(paper_id: str, include_embeddings: bool = False) -> dict:
    return intelligence.index_paper(paper_id, include_embeddings)


def search_blocks(query: str, paper_id: str | None = None, top_k: int = 8) -> dict:
    return intelligence.search_blocks(query, paper_id, top_k)


def structure_candidates(paper_id: str) -> dict:
    return intelligence.extract_structure_candidates(paper_id)


def search_graph_context(query: str, paper_id: str | None = None, top_k: int = 10) -> dict:
    return intelligence.search_approved_graph(query, paper_id, top_k)
