"""知识图谱存储层（写入）：把校验通过的抽取结果事务化写入 SQLite。

写入语义（规格 8.2 / 8.3）：
- 整体替换：同一篇论文重复抽取，先写入新证据，再删掉该论文未被本次覆盖的旧证据，
  最后清理孤儿关系/实体 —— 幂等，且失败时旧图谱完好无损（一切发生在一个事务里）。
- 每条关系/实体证据都带 paper_id + block_id，可回溯原文。
- 关系按 (source, target, relation_type) 复用，多篇论文的证据挂在同一条关系上。
"""
from ..db import db, row, rows
from ..util import dumps, loads, new_id, now
from . import kg_resolver as resolver
from .kg_schema import THIS_PAPER, norm_text


def _block_meta(pid: str) -> tuple[dict[int, str], dict[int, str]]:
    """(block_id -> 原文, block_id -> 所属章节名)。章节按 sections.block_start 边界推进。"""
    blocks = rows("SELECT id, en FROM blocks WHERE paper_id=?", (pid,))
    secs = rows("SELECT block_start, title, title_en FROM sections WHERE paper_id=? "
                "ORDER BY order_idx", (pid,))
    start_at = {s["block_start"]: s for s in secs if s["block_start"] is not None}
    texts, sec_of, cur = {}, {}, ""
    for b in sorted(blocks, key=lambda x: x["id"]):
        s = start_at.get(b["id"])
        if s:
            cur = s["title_en"] or s["title"] or ""
        texts[b["id"]] = b["en"] or ""
        sec_of[b["id"]] = cur
    return texts, sec_of


def replace_paper_extraction(pid: str, entities: list, relations: list) -> dict:
    """写入一篇论文的抽取结果（整体替换旧版本）。entities/relations 为 kg_schema
    校验后的 EntityItem / RelationItem 列表。返回统计。

    顺序保证幂等与失败安全：先在内存中收集并去重全部证据行 → 事务内删掉该论文
    全部旧证据 → 插入新证据 → 清理孤儿关系/实体。同一输入重复执行结果一致。
    """
    paper = row("SELECT title, title_zh FROM papers WHERE id=?", (pid,))
    if not paper:
        raise RuntimeError("论文不存在")
    paper_title = (paper["title_zh"] or paper["title"] or "").strip() or "未命名论文"
    block_texts, sec_of = _block_meta(pid)
    ts = now()
    stats = {"entities": 0, "relations": 0, "evidence": 0}
    pending: list[tuple] = []
    seen_keys: set = set()

    with db() as c:
        # 本文论文节点（THIS_PAPER 的落点），稳定 id 便于前端识别与跳转
        self_id = f"paper_{pid}"
        c.execute(
            "INSERT OR IGNORE INTO kg_entities(id, entity_type, canonical_name, "
            "normalized_name, description, metadata_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self_id, "Paper", paper_title[:300],
             resolver.normalize_name(paper_title)[:300], "本论文库中的论文", "{}", ts, ts))
        c.execute("UPDATE kg_entities SET updated_at=? WHERE id=?", (ts, self_id))
        mrow = c.execute("SELECT metadata_json FROM kg_entities WHERE id=?",
                         (self_id,)).fetchone()
        meta = loads(mrow[0] if mrow else "{}", {})
        if meta.get("library_paper_id") != pid:
            meta["library_paper_id"] = pid
            c.execute("UPDATE kg_entities SET metadata_json=? WHERE id=?",
                      (dumps(meta), self_id))

        cache: dict = {}
        ent_ids = {resolver.normalize_name(THIS_PAPER): self_id}

        # --- 实体 ---
        for e in entities:
            eid = resolver.resolve_entity(c, cache, e.name, e.entity_type,
                                          e.description or "", e.aliases)
            ent_ids[resolver.normalize_name(e.name)] = eid
            resolver.touch_library_paper_link(c, eid, e.entity_type, e.name)
            text = (block_texts.get(e.evidence_block_id) or e.name)[:400]
            _collect_evidence(pending, seen_keys, entity_id=eid, relation_id="",
                              paper_id=pid, block_id=e.evidence_block_id,
                              evidence_text=text,
                              section_name=sec_of.get(e.evidence_block_id, ""),
                              confidence=e.confidence, evidence_type="explicit",
                              created=ts)
            stats["entities"] += 1

        # --- 关系 ---
        for r in relations:
            src = ent_ids.get(resolver.normalize_name(r.source))
            tgt = ent_ids.get(resolver.normalize_name(r.target))
            if not src or not tgt:
                continue  # 端点实体在本次写入中缺席（理论上 schema 校验已拦截）
            rid = _upsert_relation(c, src, tgt, r.relation_type, r.evidence_type,
                                   r.confidence, pid)
            _collect_evidence(pending, seen_keys, entity_id="", relation_id=rid,
                              paper_id=pid, block_id=r.evidence_block_id,
                              evidence_text=r.evidence_text,
                              section_name=sec_of.get(r.evidence_block_id, ""),
                              confidence=r.confidence, evidence_type=r.evidence_type,
                              created=ts)
            stats["relations"] += 1

        # --- 整体替换：删旧证据 → 插新证据 → 清孤儿（同事务，失败即整体回滚） ---
        c.execute("DELETE FROM kg_evidence WHERE paper_id=?", (pid,))
        c.executemany(
            "INSERT INTO kg_evidence(id, entity_id, relation_id, paper_id, block_id, "
            "evidence_text, section_name, confidence, evidence_type, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", pending)
        resolver.purge_orphans(c)
    stats["evidence"] = len(pending)
    return stats


def _collect_evidence(pending: list, seen: set, *, entity_id, relation_id, paper_id,
                      block_id, evidence_text, section_name, confidence, evidence_type,
                      created):
    norm = norm_text(evidence_text)[:180]
    key = (paper_id, entity_id, relation_id, norm)
    if key in seen:
        return
    seen.add(key)
    pending.append((new_id(), entity_id, relation_id, paper_id, block_id,
                    (evidence_text or "")[:500], (section_name or "")[:200],
                    confidence, evidence_type, created))


def _upsert_relation(c, src, tgt, rtype, ev_type, confidence, pid) -> str:
    r = c.execute(
        "SELECT id, confidence FROM kg_relations WHERE source_entity_id=? AND "
        "target_entity_id=? AND relation_type=?", (src, tgt, rtype)).fetchone()
    if r:
        rid = r[0]
        # 同一关系多论文支持时，置信度取更优；证据类型只升不降（inferred/similarity → explicit）
        if confidence > (r[1] or 0):
            c.execute("UPDATE kg_relations SET confidence=? WHERE id=?", (confidence, rid))
        if ev_type == "explicit":
            c.execute("UPDATE kg_relations SET evidence_type='explicit' WHERE id=? "
                      "AND evidence_type!='explicit'", (rid,))
        return rid
    rid = new_id()
    c.execute(
        "INSERT INTO kg_relations(id, source_entity_id, target_entity_id, relation_type, "
        "evidence_type, confidence, status, created_by_paper, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (rid, src, tgt, rtype, ev_type, confidence, "active", pid, now()))
    return rid
