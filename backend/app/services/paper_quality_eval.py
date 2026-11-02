"""Transparent, conservative quality metrics for manually labeled paper drafts."""
from __future__ import annotations

import json
import re
from typing import Any


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", str(value or "").lower()).strip()


def evaluate_payload(payload: dict[str, Any], gold: dict[str, Any],
                     block_map: dict[int, str] | None = None) -> dict[str, Any]:
    """Return diagnostic metrics; concept recall is explicitly a lexical proxy."""
    block_map = block_map or {}
    searchable = json.dumps({
        "paper_type": payload.get("paper_type"),
        "problem": payload.get("problem"),
        "thesis": payload.get("thesis"),
        "contributions": payload.get("contributions", []),
        "theoretical_framework": payload.get("theoretical_framework", []),
        "section_map": payload.get("section_map", []),
    }, ensure_ascii=False).lower()
    concepts = [_norm(x) for x in gold.get("expected_concepts", []) if _norm(x)]
    found = [x for x in concepts if x in _norm(searchable)]
    evidence = []
    for node in payload.get("contributions", []):
        evidence.extend(node.get("evidence") or [])
    for relation in payload.get("relations", []) + payload.get("theory_logic", []):
        if relation.get("evidence"):
            evidence.append(relation["evidence"])
    valid = 0
    for item in evidence:
        try:
            bid = int(item.get("block_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        quote = str(item.get("quote") or "").strip()
        if quote and quote in block_map.get(bid, ""):
            valid += 1
    hyperedges = [x for x in payload.get("theory_logic", [])
                  if x.get("relation_type") == "JOINTLY_SUFFICIENT_FOR"]
    shaped = sum(len(set(x.get("source_ids") or [])) >= 2 for x in hyperedges)
    all_edges = payload.get("relations", []) + payload.get("theory_logic", [])
    return {
        "concept_recall_proxy": round(len(found) / max(1, len(concepts)), 4),
        "matched_concepts": found,
        "missing_concepts": [x for x in concepts if x not in found],
        "evidence_exact_match_rate": round(valid / max(1, len(evidence)), 4) if evidence else None,
        "evidence_count": len(evidence),
        "valid_evidence_count": valid,
        "joint_sufficiency_hyperedge_shape_rate": round(shaped / max(1, len(hyperedges)), 4) if hyperedges else None,
        "joint_sufficiency_hyperedge_count": len(hyperedges),
        "unverified_edge_count": sum(1 for x in all_edges if x.get("verification", "unverified") == "unverified"),
        "negative_cases_for_human_review": list(gold.get("expected_negative_cases", [])),
        "note": "concept_recall_proxy 是词面覆盖诊断，不是语义准确率；负面案例需人工标注。"
    }
