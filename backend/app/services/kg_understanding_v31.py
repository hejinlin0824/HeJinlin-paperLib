"""V3.1 chunked paper-understanding pipeline.

Keeps the legacy V3 storage/review contract while replacing lossy section truncation
with complete block-aware chunking, targeted theory backfill and auditable coverage.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..db import row, rows
from . import kg_understanding as legacy
from .translate import chat, engine_info, engine_ready

logger = logging.getLogger(__name__)
VERSION = "3.1"
CHUNK_CHARS = 6200
SYNTHESIS_CHARS = 24000
MAX_RELATIONS = 20

CHUNK_SYSTEM = """你是严谨的学术论文分块分析员。当前输入只是论文的一个连续章节片段，不能假定已看到全文。
输出合法 JSON：{"section_role":"abstract|introduction|related_work|preliminaries|method|theory|experiment|discussion|conclusion|other","summary":"本片段的学术内容摘要","claims":[{"claim":"明确主张","kind":"definition|condition|theorem|method|finding|limitation|other","evidence":[{"block_id":1,"quote":"原文连续逐字摘录"}]}],"argument_steps":["论证步骤/前提/推导/反例，按原文顺序"],"candidate_contributions":["可能属于作者贡献的内容"],"open_questions":["片段不足以判断的事项"]}。
优先保留定义、假设、必要条件、充分条件、定理、引理、证明、反例、边界案例、限制和关键结果。每个 evidence 必须逐字复制输入块，block_id 必须真实。不要把例子当定义、把案例判据升级为普遍定义，也不要推断未提供的内容。"""

SYNTHESIS_SYSTEM = """你是学术论文理解专家。依据多个章节分块摘要、论证步骤和逐字证据，综合整篇论文。
区分：formal_definition（作者正式定义）、operational_criterion（操作判据）、example_or_case（实例/反例）、non_essential_property（作者明确声明非必要的属性）、proof_or_argument（证明/论证）、background（背景）。
论文类型只能 theory/algorithm/system/empirical/survey/other。
只输出 JSON，字段：
paper_type, problem, thesis,
contributions:[{id,title,description,kind,importance(core|supporting|context),knowledge_role,parent_id,evidence:[{block_id,quote}]}],
theoretical_framework:[{name,role,evidence}],
method_architecture:[{name,description,evidence}],
key_findings:[{claim,evidence}],
limitations:[...], uncertainties:[...],
relations:[{source_id,target_id,relation_type,evidence:{block_id,quote}}],
theory_logic:[{id,relation_type,source_ids:[...],target_id,statement,evidence:{block_id,quote}}],
argument_map:[{claim, premises:[...], conclusion, evidence:[{block_id,quote}]}].
理论论文必须分别识别条件，不得把多个条件合并成一个空泛节点。必要条件与充分条件必须区分；多个条件共同充分时，必须用一个多前提逻辑关系表达，不能声称任一单条件单独充分。定义、操作判据、案例和非必要属性必须分开建模。每项关键主张需证据；不确定就写入 uncertainties。"""

LOGIC_BACKFILL_SYSTEM = """你是形式化论证审查员。仅根据给出的理论论文证据，提取明确表达的逻辑关系。
输出 JSON：{"theory_logic":[{"id":"logic1","relation_type":"HAS_NECESSARY_CONDITION|JOINTLY_SUFFICIENT_FOR|EQUIVALENT_TO|IMPLIES|NOT_REQUIRED|SPECIALIZATION_OF|EXCLUDES|CONTRADICTS","source_ids":["节点ID"],"target_id":"节点ID","statement":"中文说明","evidence":{"block_id":1,"quote":"原文连续逐字摘录"}}]}。
JOINTLY_SUFFICIENT_FOR 的 source_ids 必须包含两个或以上条件；必要条件关系是单个条件到被定义概念。不得把“由...组成”自动当作充分性证明。NOT_REQUIRED 用于作者明确说某属性不是必要条件。证据必须逐字来自输入，证据不足则不输出。"""

def _split_text(text: str, limit: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。！？;；])\s+|\n+", text)
    out, current = [], ""
    for part in parts:
        if not part:
            continue
        while len(part) > limit:
            if current:
                out.append(current); current = ""
            out.append(part[:limit]); part = part[limit:]
        if len(current) + len(part) + 1 > limit and current:
            out.append(current); current = part
        else:
            current = (current + "\n" + part).strip()
    if current:
        out.append(current)
    return out

def _load_sections(paper_id: str) -> tuple[list[dict], dict[int, str]]:
    blocks = rows("SELECT id,type,en FROM blocks WHERE paper_id=? ORDER BY order_idx", (paper_id,))
    section_rows = rows("SELECT id,title,title_en,block_start,skip FROM sections WHERE paper_id=? ORDER BY order_idx", (paper_id,))
    starts = {s["block_start"]: s for s in section_rows if s.get("block_start") is not None}
    sections, current = [], None
    block_map: dict[int, str] = {}
    for block in blocks:
        section = starts.get(block["id"])
        if section:
            title = section.get("title_en") or section.get("title") or "Untitled section"
            backmatter = bool(re.search(r"references|bibliography|acknowledg(e)?ments|参考文献|致谢", title, re.I))
            current = {"title": title, "skip": bool(section.get("skip")) or backmatter, "blocks": []}
            sections.append(current)
        if block["type"] not in ("text", "heading", "list", "table"):
            continue
        if current is None:
            current = {"title": "Abstract / 前置内容", "skip": False, "blocks": []}
            sections.append(current)
        if current["skip"]:
            continue
        text = (block.get("en") or "").strip()
        if len(text) < 25:
            continue
        block_id = int(block["id"])
        block_map[block_id] = text
        current["blocks"].append({"id": block_id, "text": text})
    return [s for s in sections if s["blocks"]], block_map

def _make_chunks(sections: list[dict]) -> list[dict]:
    chunks = []
    for section_index, section in enumerate(sections):
        current, size, chunk_index = [], 0, 0
        for block in section["blocks"]:
            for piece in _split_text(block["text"], CHUNK_CHARS - 180):
                entry = {"block_id": block["id"], "text": piece}
                if current and size + len(piece) + 80 > CHUNK_CHARS:
                    chunks.append({"section": section["title"], "section_index": section_index,
                                   "chunk_index": chunk_index, "segments": current})
                    chunk_index += 1
                    current, size = [], 0
                current.append(entry); size += len(piece) + 80
        if current:
            chunks.append({"section": section["title"], "section_index": section_index,
                           "chunk_index": chunk_index, "segments": current})
    return chunks

def _chunk_prompt(title: str, chunk: dict) -> str:
    body = "\n".join(f"<block id={x['block_id']}>\n{x['text']}\n</block>" for x in chunk["segments"])
    return f"论文：{title}\n章节：{chunk['section']}\n片段：{chunk['chunk_index'] + 1}\n{body}"

def _validate_logic(payload: dict, block_map: dict[int, str]) -> list[str]:
    warnings, accepted = [], []
    nodes = {str(n.get("id")) for n in payload.get("contributions", []) if n.get("id")}
    allowed = {"HAS_NECESSARY_CONDITION", "JOINTLY_SUFFICIENT_FOR", "EQUIVALENT_TO",
               "IMPLIES", "NOT_REQUIRED", "SPECIALIZATION_OF", "EXCLUDES", "CONTRADICTS"}
    for rel in payload.get("theory_logic", []) if isinstance(payload.get("theory_logic"), list) else []:
        if not isinstance(rel, dict):
            continue
        try:
            bid = int((rel.get("evidence") or {}).get("block_id"))
        except (TypeError, ValueError, AttributeError):
            bid = -1
        quote = str((rel.get("evidence") or {}).get("quote") or "").strip()
        raw_sources = rel.get("source_ids", [])
        sources = [str(x) for x in raw_sources] if isinstance(raw_sources, list) else []
        target = str(rel.get("target_id") or "")
        kind = str(rel.get("relation_type") or "").upper()
        valid_shape = kind in allowed and target in nodes and bool(sources) and all(x in nodes for x in sources)
        if kind == "JOINTLY_SUFFICIENT_FOR":
            valid_shape = valid_shape and len(set(sources)) >= 2
        if not valid_shape or len(quote) < 12 or quote not in block_map.get(bid, ""):
            warnings.append("一条理论逻辑关系因端点、关系类型或逐字证据无效而移除")
            continue
        accepted.append({"id": str(rel.get("id") or f"logic{len(accepted)+1}"),
                         "relation_type": kind, "source_ids": list(dict.fromkeys(sources)),
                         "target_id": target, "statement": str(rel.get("statement") or "")[:600],
                         "evidence": {"block_id": bid, "quote": quote}, "verification": "evidence_supported"})
    payload["theory_logic"] = accepted
    return warnings

async def run_understanding(draft_id: str) -> None:
    legacy.ensure_tables()
    draft = row("SELECT * FROM kg_understanding_drafts WHERE id=?", (draft_id,))
    if not draft:
        return
    info = engine_info()
    model_name = f"{info.get('provider') or 'unknown'}/{info.get('model') or 'unknown'}"
    warnings: list[str] = []
    payload: dict[str, Any] = {"version": VERSION, "section_map": [], "coverage": {}}
    stage = "engine_check"
    legacy._persist_progress(draft_id, model_name, payload, warnings)
    ok, error = engine_ready(info)
    if not ok:
        warnings.append(f"模型未就绪：{error}")
        legacy._persist_progress(draft_id, model_name, payload, warnings, "failed")
        return
    paper = row("SELECT id,title,title_zh FROM papers WHERE id=?", (draft["paper_id"],))
    if not paper:
        warnings.append("论文记录不存在")
        legacy._persist_progress(draft_id, model_name, payload, warnings, "failed")
        return
    sections, block_map = _load_sections(draft["paper_id"])
    if not sections:
        warnings.append("没有可用的已解析正文")
        legacy._persist_progress(draft_id, model_name, payload, warnings, "failed")
        return
    title = paper.get("title_zh") or paper.get("title") or "未命名论文"
    chunks = _make_chunks(sections)
    total_chars = sum(len(v) for v in block_map.values())
    total_blocks = len(block_map)
    total_chunk_chars = sum(len(x["text"]) for c in chunks for x in c["segments"])
    notes, processed_chars, failed_chunks = [], 0, []
    payload.update({"paper_title": title, "progress_stage": "chunk_analysis"})
    try:
        for index, chunk in enumerate(chunks):
            try:
                result = legacy._json(await chat(info, [
                    {"role": "system", "content": CHUNK_SYSTEM},
                    {"role": "user", "content": _chunk_prompt(title, chunk)}
                ], max_tokens=2200))
                if not result:
                    raise RuntimeError("分块分析未返回合法 JSON")
                note = {"title": chunk["section"], "chunk_index": chunk["chunk_index"],
                        "section_index": chunk["section_index"], **result}
                notes.append(note)
                processed_chars += sum(len(x["text"]) for x in chunk["segments"])
            except Exception as exc:
                failed_chunks.append({"section": chunk["section"], "chunk_index": chunk["chunk_index"],
                                      "error": type(exc).__name__})
                warnings.append(f"章节“{chunk['section']}”分块 {chunk['chunk_index']+1} 分析失败：{type(exc).__name__}")
            payload["chunk_progress"] = {"completed": index + 1, "total": len(chunks)}
            payload["coverage"] = {
                "section_count": len(sections), "chunk_count": len(chunks),
                "completed_chunks": len(notes), "failed_chunks": len(failed_chunks),
                "total_blocks": total_blocks, "total_chars": total_chars,
                "processed_chars": processed_chars,
                "chunked_chars": total_chunk_chars,
                "input_coverage": round(processed_chars / max(1, total_chunk_chars), 4),
                "complete": not failed_chunks and processed_chars >= total_chunk_chars,
            }
            payload["chunk_notes"] = notes
            legacy._persist_progress(draft_id, model_name, payload, warnings)
        if not notes:
            raise RuntimeError("所有章节分块均未能完成分析")
        # Aggregate chunk notes by chapter; preserve each chunk's argument/evidence.
        section_map = []
        for si, section in enumerate(sections):
            group = [n for n in notes if n["section_index"] == si]
            if not group:
                section_map.append({"title": section["title"], "section_role": "other",
                                    "summary": "本章分块分析失败，内容未能完整覆盖。", "claims": [],
                                    "coverage_status": "failed"})
                continue
            section_map.append({"title": section["title"], "section_role": "other",
                                "section_role": group[0].get("section_role", "other"),
                                "summary": " ".join(str(n.get("summary") or "") for n in group)[:1200],
                                "claims": [c for n in group for c in n.get("claims", [])][:20],
                                "argument_steps": [a for n in group for a in n.get("argument_steps", [])][:30],
                                "coverage_status": "complete" if len(group) == sum(1 for x in chunks if x["section_index"] == si) else "partial"})
        payload["section_map"] = section_map
        payload["progress_stage"] = "paper_synthesis"
        legacy._persist_progress(draft_id, model_name, payload, warnings)
        compact_notes = json.dumps(notes, ensure_ascii=False)
        if len(compact_notes) > SYNTHESIS_CHARS:
            # Hierarchical compression: summarize bounded batches, never silently head/tail truncate.
            batches = [compact_notes[i:i + SYNTHESIS_CHARS] for i in range(0, len(compact_notes), SYNTHESIS_CHARS)]
            reduced = []
            for batch_index, batch in enumerate(batches):
                reduced_result = legacy._json(await chat(info, [
                    {"role": "system", "content": "压缩以下论文分块摘要，保留主张、论证步骤、类型和证据 block_id/quote，不得丢弃必要/充分逻辑。只输出 JSON：{\"notes\":[...]}"},
                    {"role": "user", "content": batch}
                ], max_tokens=3000))
                if not reduced_result:
                    raise RuntimeError(f"第 {batch_index+1} 批摘要压缩失败")
                reduced.extend(reduced_result.get("notes", []))
            compact_notes = json.dumps(reduced, ensure_ascii=False)
        payload["progress_stage"] = "contribution_synthesis"
        legacy._persist_progress(draft_id, model_name, payload, warnings)
        result = legacy._json(await chat(info, [
            {"role": "system", "content": SYNTHESIS_SYSTEM},
            {"role": "user", "content": f"论文标题：{title}\n分块摘要和证据：\n{compact_notes}"}
        ], max_tokens=7500))
        if not result:
            raise RuntimeError("论文综合阶段未返回合法 JSON")
        for key in ("contributions", "theoretical_framework", "method_architecture",
                    "key_findings", "limitations", "uncertainties", "relations", "theory_logic", "argument_map"):
            result.setdefault(key, [])
        result["version"] = VERSION
        result["paper_title"] = title
        result["section_map"] = section_map
        result["coverage"] = payload["coverage"]
        result["chunk_notes"] = notes
        result["progress_stage"] = "evidence_audit"
        payload = result
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
        warnings.extend(_validate_logic(payload, block_map))
        legacy._persist_progress(draft_id, model_name, payload, warnings)
        # A separate targeted pass recovers formal logic that broad synthesis may under-specify.
        if payload.get("paper_type") == "theory" and payload.get("contributions"):
            payload["progress_stage"] = "theory_logic_backfill"
            legacy._persist_progress(draft_id, model_name, payload, warnings)
            logic_source = json.dumps({"nodes": [{"id": n["id"], "title": n.get("title"),
                "description": n.get("description"), "knowledge_role": n.get("knowledge_role")}
                for n in payload["contributions"]], "notes": notes}, ensure_ascii=False)
            logic_result = legacy._json(await chat(info, [
                {"role": "system", "content": LOGIC_BACKFILL_SYSTEM},
                {"role": "user", "content": logic_source[:SYNTHESIS_CHARS]}
            ], max_tokens=2600))
            if logic_result:
                payload["theory_logic"] = logic_result.get("theory_logic", [])
                warnings.extend(_validate_logic(payload, block_map))
        # Semantic verifier only verifies binary edge candidates; joint sufficiency stays a hyperedge.
        payload["progress_stage"] = "relation_verification"
        legacy._persist_progress(draft_id, model_name, payload, warnings)
        node_map = legacy.nodes_by_id(payload)
        for rel in payload.get("relations", [])[:MAX_RELATIONS]:
            prompt = json.dumps({"source": node_map.get(rel["source_id"]),
                "relation": rel["relation_type"], "target": node_map.get(rel["target_id"]),
                "quote": rel["evidence"]["quote"]}, ensure_ascii=False)
            verdict = legacy._json(await chat(info, [
                {"role": "system", "content": legacy.RELATION_SYSTEM},
                {"role": "user", "content": prompt}
            ], max_tokens=600))
            rel["verification"] = verdict.get("verdict", "uncertain") if verdict else "uncertain"
            rel["reason"] = str(verdict.get("reason") or "")[:500] if verdict else "验证器未返回有效结论"
            legacy._persist_progress(draft_id, model_name, payload, warnings)
        payload.pop("progress_stage", None)
        payload["coverage"]["complete"] = payload["coverage"]["failed_chunks"] == 0
        if not payload["coverage"]["complete"]:
            warnings.append("部分章节分块未能分析；本档案为不完整草稿，不能视为全文覆盖")
        legacy._persist_progress(draft_id, model_name, payload, warnings, "draft")
    except Exception as exc:
        logger.exception("V3.1 understanding failed: draft=%s paper=%s stage=%s",
                         draft_id, draft["paper_id"], payload.get("progress_stage", stage))
        warnings.append(f"V3.1理解流程失败（{type(exc).__name__}）：{str(exc)[:260]}")
        payload["progress_stage"] = payload.get("progress_stage", stage)
        payload["coverage"] = payload.get("coverage", {
            "section_count": len(sections), "chunk_count": len(chunks),
            "completed_chunks": len(notes), "failed_chunks": len(failed_chunks),
            "total_blocks": total_blocks, "total_chars": total_chars,
            "processed_chars": processed_chars,
            "input_coverage": round(processed_chars / max(1, total_chars), 4),
            "complete": False})
        legacy._persist_progress(draft_id, model_name, payload, warnings, "failed")
