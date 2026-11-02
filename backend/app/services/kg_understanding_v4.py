"""V4 orchestration: local-first indexing, bounded LLM reasoning, recoverable drafts."""
from __future__ import annotations

import json
import logging
from typing import Any

from ..db import db, row
from ..util import now
from . import kg_understanding as legacy
from . import kg_understanding_v31 as v31
from . import paper_intelligence_v4 as intelligence
from .translate import chat, engine_info, engine_ready

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
    return legacy.list_drafts(paper_id)


def get_draft(draft_id: str) -> dict | None:
    return legacy.get_draft(draft_id)


def update_review(draft_id: str, payload: dict, status: str | None = None) -> dict:
    return legacy.update_review(draft_id, payload, status)


def _fallback_payload(title: str, section_map: list[dict], candidates: list[dict]) -> dict[str, Any]:
    contributions = []
    for i, item in enumerate(candidates[:40], 1):
        contributions.append({
            "id": f"rule_{i}", "title": f"{item['kind'].title()}: {item['matched_term']}",
            "description": item["excerpt"][:500], "kind": item["kind"],
            "importance": "supporting", "knowledge_role": "proof_or_argument" if item["kind"] == "proof" else "formal_definition" if item["kind"] == "definition" else "core_claim",
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


def _compact_notes(notes: list[dict]) -> str:
    """Deterministic compression: never invokes a model and never cuts evidence quotes."""
    compact = []
    for note in notes:
        claims = []
        for claim in (note.get("claims") or [])[:8]:
            if not isinstance(claim, dict):
                continue
            evidence = [e for e in (claim.get("evidence") or [])[:2]
                        if isinstance(e, dict) and e.get("block_id") and e.get("quote")]
            claims.append({"claim": str(claim.get("claim") or "")[:500],
                           "kind": claim.get("kind"), "evidence": evidence})
        compact.append({
            "title": note.get("title"), "section_role": note.get("section_role"),
            "summary": str(note.get("summary") or "")[:900],
            "claims": claims,
            "argument_steps": [str(x)[:350] for x in (note.get("argument_steps") or [])[:8]],
            "candidate_contributions": [str(x)[:300] for x in (note.get("candidate_contributions") or [])[:5]],
            "open_questions": [str(x)[:250] for x in (note.get("open_questions") or [])[:4]],
        })
    return json.dumps(compact, ensure_ascii=False)


async def run_understanding(draft_id: str) -> None:
    legacy.ensure_tables()
    draft = row("SELECT * FROM kg_understanding_drafts WHERE id=?", (draft_id,))
    if not draft:
        return
    info = engine_info()
    model_name = f"{info.get('provider') or 'unknown'}/{info.get('model') or 'unknown'}"
    warnings: list[str] = []
    payload: dict[str, Any] = {"version": VERSION, "section_map": [], "coverage": {},
                               "pipeline": {"llm_operations": 0, "retrieval": "not_indexed"}}
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
        candidates = intelligence.extract_structure_candidates(draft["paper_id"])["candidates"]
        payload["pipeline"]["rule_candidates"] = len(candidates)
        sections, block_map = v31._load_sections(draft["paper_id"])
        chunks = v31._make_chunks(sections)
        if not chunks:
            raise ValueError("没有可分析的正文分块")
        total_chars = sum(len(x["text"]) for section in sections for x in section["blocks"])
        total_chunk_chars = sum(len(x["text"]) for chunk in chunks for x in chunk["segments"])
        notes: list[dict] = []
        failed: list[dict] = []\n        processed_chars = 0
        payload.update({"paper_title": title, "progress_stage": "chunk_analysis"})
        legacy._persist_progress(draft_id, model_name, payload, warnings)
        ready, reason = engine_ready(info)
        if not ready:
            warnings.append(f"模型未就绪：{reason}；保留本地规则候选与章节结构。")
        else:
            for index, chunk in enumerate(chunks):
                try:
                    payload["pipeline"]["llm_operations"] += 1
                    result = legacy._json(await chat(info, [
                        {"role": "system", "content": v31.CHUNK_SYSTEM},
                        {"role": "user", "content": v31._chunk_prompt(title, chunk)},
                    ], max_tokens=2200))
                    if not result:
                        raise RuntimeError("模型未返回合法 JSON")
                    notes.append({"title": chunk["section"], "chunk_index": chunk["chunk_index"],
                                  "section_index": chunk["section_index"], **result})
                except Exception as exc:
                    failed.append({"section": chunk["section"], "chunk_index": chunk["chunk_index"],
                                   "error_type": type(exc).__name__, "message": str(exc)[:240]})
                    warnings.append(f"章节“{chunk['section']}”分块 {chunk['chunk_index']+1} 失败：{type(exc).__name__}")
                payload["chunk_notes"] = notes
                payload["chunk_progress"] = {"completed": index + 1, "total": len(chunks)}
                payload["coverage"] = {
                    "section_count": len(sections), "chunk_count": len(chunks),
                    "completed_chunks": len(notes), "failed_chunks": len(failed),
                    "total_blocks": len(block_map), "total_chars": total_chars,
                    "processed_chars": processed_chars,
                    "chunked_chars": total_chunk_chars,
                    "input_coverage": round(processed_chars / max(1, total_chunk_chars), 4),
                    "complete": len(failed) == 0,
                }
                payload["failed_chunks"] = failed
                legacy._persist_progress(draft_id, model_name, payload, warnings)
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
            compact_notes = _compact_notes(notes)
            try:
                payload["pipeline"]["llm_operations"] += 1
                result = legacy._json(await chat(info, [
                    {"role": "system", "content": v31.SYNTHESIS_SYSTEM},
                    {"role": "user", "content": f"论文标题：{title}\\n章节分块分析（证据均带 block_id）：\\n{compact_notes}"},
                ], max_tokens=7500))
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
            except Exception as exc:
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
            "failed_chunks": len(failed), "total_blocks": len(block_map), "total_chars": total_chars,
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
                            "non_essential_property", "proof_or_argument", "background", "core_claim"}:
                node["knowledge_role"] = "core_claim" if node.get("importance") == "core" else "proof_or_argument"
        if payload.get("paper_type") not in {"theory", "algorithm", "system", "empirical", "survey", "other"}:
            payload["paper_type"] = "other"
        warnings.extend(legacy._normalise_relations(payload, block_map))
        warnings.extend(v31._validate_logic(payload, block_map))
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
            try:
                payload["pipeline"]["llm_operations"] += 1
                verdicts = legacy._json(await chat(info, [
                    {"role": "system", "content": "你是学术关系语义核验器。逐条判断证据是否蕴含关系。联合充分关系必须把所有前提作为整体检查。不得依赖常识补足证据。只输出 JSON，格式为 verifications 数组，每项包含 id、verdict（entailed/not_entailed/uncertain）、reason。"},
                    {"role": "user", "content": json.dumps({"edges": edges}, ensure_ascii=False)},
                ], max_tokens=2200))
                by_id = {str(x.get("id")): x for x in (verdicts or {}).get("verifications", []) if isinstance(x, dict)}
                for collection in (payload.get("relations", []), payload.get("theory_logic", [])):
                    for edge in collection:
                        verdict = by_id.get(str(edge.get("id") or ""))
                        if verdict and verdict.get("verdict") in {"entailed", "not_entailed", "uncertain"}:
                            edge["verification"] = verdict["verdict"]
                            edge["reason"] = str(verdict.get("reason") or "")[:500]
            except Exception as exc:
                warnings.append(f"批量关系核验失败，保留未核验候选：{type(exc).__name__}")
        for relation in payload.get("relations", []):
            relation.setdefault("verification", "unverified")
        for relation in payload.get("theory_logic", []):
            relation.setdefault("verification", "unverified")
        payload.pop("progress_stage", None)
        if failed:
            warnings.append("部分分块未完成；其余结果已保留，档案为不完整草稿。")
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
