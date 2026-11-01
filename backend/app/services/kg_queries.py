"""知识图谱查询层（只读）：图谱视图、实体/关系详情、单篇知识结构、主题聚类。

所有列表查询都有数量上限；证据行带 stale 标记（重解析后 block_id 失效时前端禁用定位）。
"""
from ..db import row, rows
from .kg_schema import ENTITY_TYPES

MAX_NODES = 400


def overview() -> dict:
    ent_counts = {r["entity_type"]: r["n"] for r in rows(
        "SELECT entity_type, COUNT(*) n FROM kg_visible_entities GROUP BY entity_type")}
    rel_counts = {r["relation_type"]: r["n"] for r in rows(
        "SELECT relation_type, COUNT(*) n FROM kg_visible_relations GROUP BY relation_type")}
    job_counts = {r["status"]: r["n"] for r in rows(
        "SELECT status, COUNT(*) n FROM kg_visible_jobs GROUP BY status")}
    return {
        "papers_total": row("SELECT COUNT(*) n FROM papers WHERE COALESCE(library_role, 'normal')='normal'")["n"],
        "papers_extracted": row(
            "SELECT COUNT(DISTINCT paper_id) n FROM kg_visible_evidence "
            "WHERE IFNULL(entity_id,'')!=''")["n"],
        "entities_total": sum(ent_counts.values()),
        "entities": {t: ent_counts.get(t, 0) for t in sorted(ENTITY_TYPES) if ent_counts.get(t)},
        "relations_total": sum(rel_counts.values()),
        "relations": rel_counts,
        "inferred_relations": row(
            "SELECT COUNT(*) n FROM kg_visible_relations WHERE evidence_type='inferred'")["n"],
        "similarity_relations": row(
            "SELECT COUNT(*) n FROM kg_visible_relations WHERE evidence_type='similarity'")["n"],
        "evidence_total": row("SELECT COUNT(*) n FROM kg_visible_evidence")["n"],
        "jobs": job_counts,
    }


def graph(paper_id: str | None = None, entity_ids: list[str] | None = None,
          types: list[str] | None = None, relation_types: list[str] | None = None,
          evidence_types: list[str] | None = None,
          include_rejected: bool = False,
          q: str | None = None, limit: int = 250) -> dict:
    """Query a graph. By default only source-explicit relations are shown.

    Inferred and similarity edges are opt-in because they are hypotheses, not facts.
    """
    limit = max(20, min(int(limit or 250), MAX_NODES))
    types = [t for t in (types or []) if t in ENTITY_TYPES] or None
    relation_types = relation_types or None
    allowed_evidence = {"explicit", "inferred", "similarity"}
    evidence_types = (["explicit"] if evidence_types is None else
                      [t for t in evidence_types if t in allowed_evidence])
    ids: list[str] = []

    def _type_filter(selected: list[str]) -> list[str]:
        if not types:
            return selected
        keep = {r["id"] for r in rows(
            f"SELECT id FROM kg_visible_entities WHERE id IN ({','.join('?' * len(selected))}) "
            "AND entity_type IN (" + ",".join("?" * len(types)) + ")",
            (*selected, *types))}
        return [i for i in selected if i in keep]

    if entity_ids:
        ids = entity_ids[:200]
    elif paper_id:
        ids = [r["entity_id"] for r in rows(
            "SELECT DISTINCT entity_id FROM kg_visible_evidence WHERE paper_id=? AND "
            "IFNULL(entity_id,'')!=''", (paper_id,))]
        rel_ids = [r["rid"] for r in rows(
            "SELECT DISTINCT relation_id rid FROM kg_visible_evidence WHERE paper_id=? AND "
            "IFNULL(relation_id,'')!=''", (paper_id,))]
        if rel_ids:
            marks = ",".join("?" * len(rel_ids))
            for r in rows(f"SELECT source_entity_id sid, target_entity_id tid FROM "
                          f"kg_visible_relations WHERE id IN ({marks})", rel_ids):
                ids += [r["sid"], r["tid"]]
        ids = list(dict.fromkeys(x for x in ids if x))
    elif q:
        like = f"%{(q or '').strip()}%"
        hits = rows("SELECT id FROM kg_visible_entities WHERE canonical_name LIKE ? OR "
                    "normalized_name LIKE ? ORDER BY LENGTH(canonical_name) LIMIT 40",
                    (like, like))
        ids = [h["id"] for h in hits]
        if ids:
            marks = ",".join("?" * len(ids))
            for r in rows(f"SELECT source_entity_id sid, target_entity_id tid FROM "
                          f"kg_visible_relations WHERE source_entity_id IN ({marks}) "
                          f"OR target_entity_id IN ({marks})", (*ids, *ids)):
                ids += [r["sid"], r["tid"]]
        ids = list(dict.fromkeys(ids))[:limit]
    else:
        # 全局视图：按连接度取核心节点（限流渲染），库内论文节点始终保留
        top = rows(
            "SELECT e.id, (SELECT COUNT(*) FROM kg_visible_relations r WHERE "
            "r.source_entity_id=e.id OR r.target_entity_id=e.id) deg "
            "FROM kg_visible_entities e ORDER BY deg DESC LIMIT ?", (limit,))
        ids = [r["id"] for r in top]
        papers = rows("SELECT id FROM kg_visible_entities WHERE id LIKE 'paper_%' LIMIT 100")
        ids = list(dict.fromkeys(ids + [p["id"] for p in papers]))

    if not ids:
        return {"nodes": [], "edges": [], "total_entities": _total_entities()}

    ids = _type_filter(ids)[:MAX_NODES]
    marks = ",".join("?" * len(ids))
    nodes = rows(
        f"SELECT e.id, e.entity_type, e.canonical_name, e.name_zh, e.description, e.metadata_json, "
        f"(SELECT COUNT(*) FROM kg_visible_relations r WHERE r.source_entity_id=e.id OR "
        f"r.target_entity_id=e.id) deg FROM kg_visible_entities e WHERE e.id IN ({marks})", ids)
    rel_filter, rel_args = "", []
    if relation_types:
        rel_filter += " AND r.relation_type IN (" + ",".join("?" * len(relation_types)) + ")"
        rel_args.extend(relation_types)
    if not evidence_types:
        rel_filter += " AND 1=0"
    else:
        rel_filter += " AND r.evidence_type IN (" + ",".join("?" * len(evidence_types)) + ")"
        rel_args.extend(evidence_types)
    if not include_rejected:
        rel_filter += " AND r.review_status!='rejected'"
    edges = rows(
        f"SELECT r.id, r.source_entity_id, r.target_entity_id, r.relation_type, "
        f"r.evidence_type, r.confidence, r.review_status, "
        f"(SELECT COUNT(*) FROM kg_visible_evidence ev WHERE ev.relation_id=r.id) ev_count, "
        f"(SELECT GROUP_CONCAT(DISTINCT paper_id) FROM kg_visible_evidence ev WHERE "
        f"ev.relation_id=r.id) papers, "
        f"(SELECT ev.evidence_text FROM kg_visible_evidence ev WHERE ev.relation_id=r.id "
        f"AND ev.evidence_type=r.evidence_type ORDER BY ev.confidence DESC, ev.created_at DESC LIMIT 1) evidence_text, "
        f"(SELECT ev.section_name FROM kg_visible_evidence ev WHERE ev.relation_id=r.id "
        f"AND ev.evidence_type=r.evidence_type ORDER BY ev.confidence DESC, ev.created_at DESC LIMIT 1) section_name, "
        f"(SELECT ev.paper_id FROM kg_visible_evidence ev WHERE ev.relation_id=r.id "
        f"AND ev.evidence_type=r.evidence_type ORDER BY ev.confidence DESC, ev.created_at DESC LIMIT 1) evidence_paper_id "
        f"FROM kg_visible_relations r "
        f"WHERE r.source_entity_id IN ({marks}) AND r.target_entity_id IN ({marks})"
        f"{rel_filter}", (*ids, *ids, *rel_args))
    node_set = {n["id"] for n in nodes}
    import json as _json
    out_nodes = [{
        "id": n["id"], "label": n["canonical_name"],
        "label_zh": n["name_zh"] or "", "type": n["entity_type"],
        "degree": n["deg"], "description": n["description"] or "",
        "is_library_paper": n["id"].startswith("paper_") or bool(
            (_json.loads(n["metadata_json"] or "{}") or {}).get("library_paper_id")),
    } for n in nodes if n["id"] in node_set]
    out_edges = [{
        "id": e["id"], "source": e["source_entity_id"], "target": e["target_entity_id"],
        "relation_type": e["relation_type"], "evidence_type": e["evidence_type"],
        "confidence": e["confidence"], "evidence_count": e["ev_count"],
        "review_status": e["review_status"],
        "paper_ids": (e["papers"] or "").split(",") if e["papers"] else [],
        "evidence_text": e["evidence_text"] or "",
        "section_name": e["section_name"] or "",
        "evidence_paper_id": e["evidence_paper_id"] or "",
    } for e in edges]
    return {"nodes": out_nodes, "edges": out_edges, "total_entities": _total_entities()}


def _total_entities() -> int:
    return row("SELECT COUNT(*) n FROM kg_visible_entities")["n"]


_EVI_SQL = """
SELECT ev.*, p.title AS paper_title, p.title_zh AS paper_title_zh,
       CASE WHEN ev.block_id IS NOT NULL AND EXISTS(
         SELECT 1 FROM blocks b WHERE b.id=ev.block_id AND b.paper_id=ev.paper_id
       ) THEN 0 ELSE 1 END AS stale
FROM kg_visible_evidence ev JOIN papers p ON p.id=ev.paper_id
WHERE {cond}
ORDER BY ev.created_at DESC LIMIT ?
"""


def _evi_out(e: dict) -> dict:
    return {"id": e["id"], "paper_id": e["paper_id"],
            "paper_title": e["paper_title_zh"] or e["paper_title"],
            "block_id": e["block_id"], "evidence_text": e["evidence_text"],
            "section_name": e["section_name"], "confidence": e["confidence"],
            "evidence_type": e["evidence_type"], "stale": bool(e["stale"]),
            "created_at": e["created_at"]}


def entity_detail(eid: str) -> dict | None:
    e = row("SELECT * FROM kg_visible_entities WHERE id=?", (eid,))
    if not e:
        return None
    aliases = [a["alias"] for a in rows(
        "SELECT alias FROM kg_entity_aliases WHERE entity_id=? ORDER BY id LIMIT 30", (eid,))]
    evidence = [_evi_out(x) for x in rows(_EVI_SQL.format(cond="ev.entity_id=?"), (eid, 50))]
    relations = rows(
        "SELECT r.*, s.canonical_name AS source_name, s.name_zh AS source_name_zh, "
        "s.entity_type AS source_type, "
        "t.canonical_name AS target_name, t.name_zh AS target_name_zh, "
        "t.entity_type AS target_type, "
        "(SELECT COUNT(*) FROM kg_visible_evidence ev WHERE ev.relation_id=r.id) evidence_count "
        "FROM kg_visible_relations r JOIN kg_visible_entities s ON s.id=r.source_entity_id "
        "JOIN kg_visible_entities t ON t.id=r.target_entity_id "
        "WHERE (r.source_entity_id=? OR r.target_entity_id=?) AND r.review_status!='rejected' "
        "ORDER BY r.confidence DESC LIMIT 100", (eid, eid))
    import json as _json
    meta = _json.loads(e["metadata_json"] or "{}") or {}
    return {
        "id": e["id"], "entity_type": e["entity_type"], "name": e["canonical_name"],
        "name_zh": e["name_zh"] or "", "description": e["description"] or "", "aliases": aliases,
        "library_paper_id": meta.get("library_paper_id") or (
            e["id"][6:] if e["id"].startswith("paper_") else None),
        "evidence": evidence, "relations": relations,
    }


def neighbors(eid: str, limit: int = 60) -> dict:
    limit = max(10, min(int(limit), 120))
    rels = rows(
        "SELECT r.*, s.canonical_name AS source_name, s.entity_type AS source_type, "
        "t.canonical_name AS target_name, t.entity_type AS target_type "
        "FROM kg_visible_relations r JOIN kg_visible_entities s ON s.id=r.source_entity_id "
        "JOIN kg_visible_entities t ON t.id=r.target_entity_id "
        "WHERE (r.source_entity_id=? OR r.target_entity_id=?) AND r.review_status!='rejected' LIMIT ?", (eid, eid, limit))
    return {"relations": rels}


def relation_detail(rid: str) -> dict | None:
    r = row(
        "SELECT r.*, s.canonical_name AS source_name, s.name_zh AS source_name_zh, "
        "s.entity_type AS source_type, "
        "t.canonical_name AS target_name, t.name_zh AS target_name_zh, "
        "t.entity_type AS target_type "
        "FROM kg_visible_relations r JOIN kg_visible_entities s ON s.id=r.source_entity_id "
        "JOIN kg_visible_entities t ON t.id=r.target_entity_id WHERE r.id=?", (rid,))
    if not r:
        return None
    evidence = [_evi_out(x) for x in rows(_EVI_SQL.format(cond="ev.relation_id=?"), (rid, 50))]
    return {
        "id": r["id"], "relation_type": r["relation_type"],
        "evidence_type": r["evidence_type"], "confidence": r["confidence"],
        "review_status": r["review_status"],
        "source": {"id": r["source_entity_id"], "name": r["source_name"],
                   "name_zh": r["source_name_zh"] or "", "type": r["source_type"]},
        "target": {"id": r["target_entity_id"], "name": r["target_name"],
                   "name_zh": r["target_name_zh"] or "", "type": r["target_type"]},
        "evidence": evidence,
    }


def paper_structure(pid: str) -> dict | None:
    """单篇论文知识结构：研究问题 → 方法 → 使用资源 → 发现 → 相关工作，全部带证据定位。"""
    p = row("SELECT id, title, title_zh, status FROM papers WHERE id=? AND COALESCE(library_role, 'normal')='normal'", (pid,))
    if not p:
        return None
    job = row("SELECT * FROM kg_visible_jobs WHERE paper_id=? "
              "ORDER BY updated_at DESC LIMIT 1", (pid,))
    rels = rows(
        "SELECT r.id AS rid, r.relation_type, r.evidence_type, r.confidence, "
        "t.id AS entity_id, t.canonical_name AS name, t.name_zh AS name_zh, "
        "t.entity_type AS entity_type, "
        "t.description, ev.block_id, ev.evidence_text, ev.section_name "
        "FROM kg_visible_relations r JOIN kg_visible_entities t ON t.id=r.target_entity_id "
        "JOIN kg_visible_evidence ev ON ev.relation_id=r.id "
        "WHERE ev.paper_id=? AND r.evidence_type='explicit' AND r.review_status!='rejected' "
        "ORDER BY r.relation_type, r.confidence DESC", (pid,))
    buckets = {"problems": {"ADDRESSES", "MITIGATES"}, "proposals": {"PROPOSES"},
               "datasets": {"USES", "EVALUATED_ON"}, "models": set(), "metrics": {"USES_METRIC"},
               "findings": {"REPORTS"}, "related": {"COMPARES", "EXTENDS"},
               "areas": {"BELONGS_TO"}, "people": {"AUTHORED_BY", "PUBLISHED_IN"}}
    out = {k: [] for k in buckets}
    seen = set()
    for r in rels:
        key = f"{r['rid']}:{r['entity_id']}"
        if key in seen:
            continue
        seen.add(key)
        item = {"entity_id": r["entity_id"], "name": r["name"],
                "name_zh": r["name_zh"] or "",
                "entity_type": r["entity_type"], "relation_id": r["rid"],
                "relation_type": r["relation_type"], "evidence_type": r["evidence_type"],
                "block_id": r["block_id"], "evidence_text": r["evidence_text"],
                "section_name": r["section_name"], "description": r["description"] or ""}
        for bucket, types in buckets.items():
            if r["relation_type"] in types and (
                    bucket not in ("datasets", "models")
                    or (bucket == "datasets" and r["entity_type"] == "Dataset")
                    or (bucket == "models" and r["entity_type"] == "Model")):
                out[bucket].append(item)
                break
    job_out = None
    if job:
        job_out = {"id": job["id"], "status": job["status"], "stage": job["stage"],
                   "progress": job["progress"], "error": job["error_message"],
                   "updated_at": job["updated_at"]}
    return {"paper": p, "job": job_out, **out}


def topics() -> dict:
    """主题聚类（P3-lite，全部来自库内真实关系）：研究领域 → 论文/关键实体；
    只把明确抽取出的 ResearchArea 作为主题；共享数据集单独作为实验资源交集展示。"""
    area_rows = rows(
        "SELECT t.id AS area_id, t.canonical_name AS name, ev.paper_id, "
        "r.source_entity_id AS paper_entity_id "
        "FROM kg_visible_relations r JOIN kg_visible_entities t ON t.id=r.target_entity_id "
        "JOIN kg_visible_evidence ev ON ev.relation_id=r.id "
        "WHERE r.relation_type='BELONGS_TO' AND r.evidence_type='explicit' AND r.review_status!='rejected' AND t.entity_type='ResearchArea'")
    grouped: dict[str, dict] = {}
    for r in area_rows:
        g = grouped.setdefault(r["area_id"], {"name": r["name"], "papers": set(),
                                              "paper_entities": set()})
        if r["paper_id"]:
            g["papers"].add(r["paper_id"])
        g["paper_entities"].add(r["paper_entity_id"])
    out = []
    for g in grouped.values():
        papers = sorted(g["papers"])
        entity_ids, key_rels = [], []
        if g["paper_entities"]:
            marks = ",".join("?" * len(g["paper_entities"]))
            ents = rows(
                f"SELECT e.id, e.canonical_name, e.entity_type, "
                f"(SELECT COUNT(*) FROM kg_visible_relations r WHERE r.source_entity_id=e.id "
                f"OR r.target_entity_id=e.id) deg FROM kg_visible_entities e "
                f"WHERE e.id IN ({marks}) ORDER BY deg DESC LIMIT 12",
                tuple(g["paper_entities"]))
            entity_ids = [e["id"] for e in ents]
            if entity_ids:
                m2 = ",".join("?" * len(entity_ids))
                key_rels = rows(
                    f"SELECT r.id, r.relation_type, r.evidence_type, "
                    f"s.canonical_name AS source_name, t.canonical_name AS target_name "
                    f"FROM kg_visible_relations r JOIN kg_visible_entities s ON s.id=r.source_entity_id "
                    f"JOIN kg_visible_entities t ON t.id=r.target_entity_id "
                    f"WHERE r.source_entity_id IN ({m2}) AND r.target_entity_id IN ({m2}) "
                    f"AND r.evidence_type='explicit' AND r.review_status!='rejected' "
                    f"ORDER BY r.confidence DESC LIMIT 15", (*entity_ids, *entity_ids))
        out.append({"name": g["name"], "paper_count": len(papers), "paper_ids": papers,
                    "entity_ids": entity_ids, "key_relations": key_rels})
    out.sort(key=lambda x: -x["paper_count"])
    # 共享数据集是实验资源交集，不是研究主题；单独呈现，避免误导用户。
    resources = rows(
        "SELECT t.id, t.canonical_name, COUNT(DISTINCT ev.paper_id) paper_count, "
        "GROUP_CONCAT(DISTINCT ev.paper_id) papers "
        "FROM kg_visible_relations r JOIN kg_visible_entities t ON t.id=r.target_entity_id "
        "JOIN kg_visible_evidence ev ON ev.relation_id=r.id "
        "WHERE r.relation_type='USES' AND r.evidence_type='explicit' "
        "AND r.review_status!='rejected' AND t.entity_type='Dataset' "
        "GROUP BY t.id HAVING paper_count>=2 ORDER BY paper_count DESC LIMIT 12")
    shared_resources = [{
        "id": d["id"], "name": d["canonical_name"], "paper_count": d["paper_count"],
        "paper_ids": (d["papers"] or "").split(","),
    } for d in resources]
    return {"topics": out[:20], "shared_resources": shared_resources}


def jobs_list(limit: int = 50) -> list:
    return rows(
        "SELECT j.*, p.title AS paper_title, p.title_zh AS paper_title_zh "
        "FROM kg_visible_jobs j LEFT JOIN papers p ON p.id=j.paper_id "
        "ORDER BY j.updated_at DESC LIMIT ?", (max(1, min(int(limit), 200)),))
