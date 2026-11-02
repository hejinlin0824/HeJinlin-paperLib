"""Shared, deterministic paper-understanding primitives used by V3.1 and V4."""
from __future__ import annotations

import json
import re
from ..db import rows

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
    allowed = {"HAS_NECESSARY_CONDITION", "NECESSARY_FOR", "JOINTLY_SUFFICIENT_FOR", "EQUIVALENT_TO",
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

def compact_chunk_notes(notes: list[dict], max_chars: int = 48000) -> tuple[str, dict]:
    """Bound synthesis input while retaining a compact record for every analyzed chunk.

    The function progressively drops low-priority detail before shortening summaries.
    Evidence quotes and block IDs are retained preferentially; no model call is made.
    """
    if not notes:
        return "[]", {"input_chars": 2, "output_chars": 2, "chunks_included": 0,
                      "chunks_total": 0, "truncated": False, "evidence_items": 0}
    source_chars = sum(len(json.dumps(note, ensure_ascii=False, default=str)) for note in notes)
    per_note = max(120, (max_chars - 2) // len(notes))
    compact = []
    evidence_items = 0
    truncated = False
    for note in notes:
        claims = []
        if (len(str(note.get("summary") or "")) > 420
                or len(note.get("claims") or []) > 2
                or len(note.get("argument_steps") or []) > 2
                or len(note.get("candidate_contributions") or []) > 2
                or len(note.get("open_questions") or []) > 1):
            truncated = True
        for claim in (note.get("claims") or [])[:2]:
            if not isinstance(claim, dict):
                continue
            evidence = []
            if len(str(claim.get("claim") or "")) > 180 or len(claim.get("evidence") or []) > 1:
                truncated = True
            for ev in (claim.get("evidence") or [])[:1]:
                if isinstance(ev, dict) and ev.get("block_id") and ev.get("quote"):
                    if len(str(ev.get("quote") or "")) > 180:
                        truncated = True
                    evidence.append({"block_id": ev["block_id"], "quote": str(ev["quote"])[:180]})
            evidence_items += len(evidence)
            claims.append({"claim": str(claim.get("claim") or "")[:180],
                           "kind": claim.get("kind"), "evidence": evidence})
        compact.append({
            "title": str(note.get("title") or "")[:100],
            "section_index": note.get("section_index"),
            "chunk_index": note.get("chunk_index"),
            "section_role": note.get("section_role"),
            "summary": str(note.get("summary") or "")[:min(420, max(40, per_note - 260))],
            "claims": claims,
            "argument_steps": [str(x)[:120] for x in (note.get("argument_steps") or [])[:2]],
            "candidate_contributions": [str(x)[:120] for x in (note.get("candidate_contributions") or [])[:2]],
            "open_questions": [str(x)[:100] for x in (note.get("open_questions") or [])[:1]],
        })
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    # Deterministically shed optional detail in a fixed order until under budget.
    for field in ("open_questions", "candidate_contributions", "argument_steps"):
        if len(encoded) <= max_chars:
            break
        for item in compact:
            if item[field]:
                truncated = True
            item[field] = []
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        for item in compact:
            if len(item["claims"]) > 1:
                truncated = True
            item["claims"] = item["claims"][:1]
            if item["claims"]:
                if len(item["claims"][0]["claim"]) > 100 or len(item["claims"][0]["evidence"]) > 1:
                    truncated = True
                item["claims"][0]["claim"] = item["claims"][0]["claim"][:100]
                item["claims"][0]["evidence"] = item["claims"][0]["evidence"][:1]
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        summary_budget = max(8, (max_chars - len(encoded) + sum(len(x["summary"]) for x in compact)) // len(compact))
        for item in compact:
            if len(item["summary"]) > summary_budget:
                truncated = True
            item["summary"] = item["summary"][:summary_budget]
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        # Section role is repeated across chunks; section_index/title already preserve grouping.
        for item in compact:
            if item.get("section_role"):
                truncated = True
            item.pop("section_role", None)
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        # Keep the claim and verbatim source quote, but omit the redundant claim category.
        for item in compact:
            for claim in item["claims"]:
                if "kind" in claim:
                    truncated = True
                claim.pop("kind", None)
                for evidence in claim["evidence"]:
                    evidence["quote"] = evidence["quote"][:120]
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        # At very small budgets, shorten prose fields while keeping chunk identity and evidence anchors.
        for item in compact:
            item["title"] = item["title"][:48]
            for claim in item["claims"]:
                if len(claim["claim"]) > 72 or any(len(ev["quote"]) > 80 for ev in claim["evidence"]):
                    truncated = True
                claim["claim"] = claim["claim"][:72]
                for evidence in claim["evidence"]:
                    evidence["quote"] = evidence["quote"][:80]
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        # Last-resort compact form still carries every chunk's section/chunk and block IDs.
        for item in compact:
            item["summary"] = item["summary"][:8]
            item["section_role"] = ""
            item["title"] = item["title"][:24]
            for claim in item["claims"]:
                claim["claim"] = claim["claim"][:32]
                for evidence in claim["evidence"]:
                    evidence["quote"] = evidence["quote"][:40]
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    stats = {
        "input_chars": source_chars, "output_chars": len(encoded),
        "chunks_included": len(compact), "chunks_total": len(notes),
        "truncated": truncated,
        "evidence_items": evidence_items,
        "max_chars": max_chars,
    }
    return encoded, stats
