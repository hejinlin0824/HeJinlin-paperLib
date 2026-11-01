"""实体消歧与归一化（Entity Resolver）。

匹配策略（按顺序，前一级命中即复用）：
1. 批内缓存（同一次抽取内同名同类型只建一个节点）；
2. 精确匹配：同类型 + normalized_name 完全一致；
3. 别名匹配：normalized_alias 命中且实体类型一致（复用并把新名字登记为别名）；
4. 保守模糊匹配：仅限多词名称（含空格）、同类型、相似度 ≥0.93 或互为包含——
   单词名（FedAvg / CIFAR-10）永不模糊合并，避免 CIFAR-10 误并入 CIFAR-100 这类事故。

设计原则：宁可暂时保留两个节点，也不错误合并（规格 5.6）。
LLM 消歧确认暂不启用（节省 token 且规则 1~4 已足够保守），后续需要再加。
"""
import re
from difflib import SequenceMatcher

from ..db import db
from ..util import dumps, loads, new_id, now

_WS = re.compile(r"\s+")


def normalize_name(s: str) -> str:
    """名称归一：小写、压缩空白、去首尾标点。保留大小写语义由 canonical_name 承担。"""
    t = _WS.sub(" ", str(s or "").strip()).lower()
    return t.strip(" .:;,-\"'()[]")


def _fuzzy_merge_ok(a: str, b: str) -> bool:
    """保守判定两个多词名称是否同一实体（单复数等微小变体；阈值 0.92，
    "Statistical Heterogeneity/Heterogeneities" 可合，"Communication
    Efficiency/Inefficiency" 0.90 不合）。"""
    if " " not in a or " " not in b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.92


def resolve_entity(cur, cache: dict, name: str, entity_type: str,
                   description: str = "", aliases: list[str] | None = None) -> str:
    """在事务内解析/创建实体，返回实体 id。cache: {(type, norm): id}。"""
    norm = normalize_name(name)
    key = (entity_type, norm)
    if key in cache:
        return cache[key]
    ts = now()

    def _rowid(sql, args):
        r = cur.execute(sql, args).fetchone()
        return r[0] if r else None

    eid = _rowid("SELECT id FROM kg_entities WHERE entity_type=? AND normalized_name=?",
                 (entity_type, norm))
    via_alias = False
    if not eid:
        eid = _rowid(
            "SELECT a.entity_id FROM kg_entity_aliases a "
            "JOIN kg_entities e ON e.id=a.entity_id "
            "WHERE a.normalized_alias=? AND e.entity_type=? LIMIT 1",
            (norm, entity_type))
        via_alias = eid is not None
    if not eid:
        # 保守模糊合并：只对多词名称生效
        for r in cur.execute(
                "SELECT id, normalized_name FROM kg_entities WHERE entity_type=?",
                (entity_type,)).fetchall():
            if _fuzzy_merge_ok(norm, r[1]):
                eid = r[0]
                break
    if eid:
        # 复用：新写法登记为别名；描述为空时补上
        cur.execute("INSERT OR IGNORE INTO kg_entity_aliases(entity_id, alias, "
                    "normalized_alias, source) VALUES(?,?,?,?)",
                    (eid, name, norm, "resolver"))
        for a in aliases or []:
            an = normalize_name(a)
            if an:
                cur.execute("INSERT OR IGNORE INTO kg_entity_aliases(entity_id, alias, "
                            "normalized_alias, source) VALUES(?,?,?,?)",
                            (eid, a, an, "extraction"))
        if description:
            cur.execute("UPDATE kg_entities SET description=? WHERE id=? AND "
                        "(description='' OR description IS NULL)", (description, eid))
        cur.execute("UPDATE kg_entities SET updated_at=? WHERE id=?", (ts, eid))
    else:
        eid = new_id()
        cur.execute(
            "INSERT INTO kg_entities(id, entity_type, canonical_name, normalized_name, "
            "description, metadata_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (eid, entity_type, name, norm, description or "", "{}", ts, ts))
        for a in [name] + list(aliases or []):
            an = normalize_name(a)
            if an:
                cur.execute("INSERT OR IGNORE INTO kg_entity_aliases(entity_id, alias, "
                            "normalized_alias, source) VALUES(?,?,?,?)",
                            (eid, a, an, "resolver" if a == name else "extraction"))
    cache[key] = eid
    return eid


def touch_library_paper_link(cur, entity_id: str, entity_type: str, canonical_name: str):
    """Paper 实体若与库内某篇论文同名，记入 metadata.library_paper_id，前端可跳阅读器。"""
    if entity_type != "Paper":
        return
    norm = normalize_name(canonical_name)
    if len(norm) < 8:
        return
    for r in cur.execute("SELECT id, title FROM papers").fetchall():
        if normalize_name(r[1]) == norm:
            row = cur.execute("SELECT metadata_json FROM kg_entities WHERE id=?",
                              (entity_id,)).fetchone()
            meta = loads(row[0] if row else "{}", {})
            if meta.get("library_paper_id") != r[0]:
                meta["library_paper_id"] = r[0]
                cur.execute("UPDATE kg_entities SET metadata_json=? WHERE id=?",
                            (dumps(meta), entity_id))
            return


def purge_orphans(cur):
    """删除失去全部证据的关系，再删除既无证据又无关系引用的实体（及其别名）。

    必须在替换某篇论文证据之后调用；跨论文复用的实体因仍有他篇证据而自然存活。
    """
    cur.execute("DELETE FROM kg_relations WHERE id NOT IN "
                "(SELECT relation_id FROM kg_evidence WHERE IFNULL(relation_id,'')!='')")
    cur.execute("DELETE FROM kg_entity_aliases WHERE entity_id NOT IN "
                "(SELECT id FROM kg_entities)")
    cur.execute(
        "DELETE FROM kg_entities WHERE id NOT IN "
        "(SELECT entity_id FROM kg_evidence WHERE IFNULL(entity_id,'')!='') "
        "AND id NOT IN (SELECT source_entity_id FROM kg_relations) "
        "AND id NOT IN (SELECT target_entity_id FROM kg_relations)")


def drop_paper_data(pid: str):
    """删除论文时清掉它的图谱证据与抽取任务，再清理孤儿。"""
    with db() as c:
        c.execute("DELETE FROM kg_evidence WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM kg_extraction_jobs WHERE paper_id=?", (pid,))
        purge_orphans(c)
