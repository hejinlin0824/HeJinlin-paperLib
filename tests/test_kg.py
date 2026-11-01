"""知识图谱（P0/P1）回归测试：Schema 校验、实体消歧、存储幂等、任务生命周期。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import db as app_db  # noqa: E402
from app.services import kg_extract, kg_queries, kg_resolver, kg_schema, kg_store  # noqa: E402
from app.util import now  # noqa: E402

BLOCKS = [
    # id=1
    "FedProx is a federated optimization framework for heterogeneous networks. "
    "It addresses statistical heterogeneity in cross-device settings.",
    # id=2
    "We evaluate FedProx on CIFAR-10 and MNIST and report accuracy improvements over FedAvg.",
]

LLM_GOOD = {
    "entities": [
        {"name": "FedProx", "entity_type": "Algorithm",
         "description": "联邦优化框架", "aliases": ["Federated Proximal"],
         "evidence_block_id": 1, "confidence": 0.95},
        {"name": "Statistical Heterogeneity", "entity_type": "ResearchProblem",
         "description": "数据异构", "aliases": [], "evidence_block_id": 1, "confidence": 0.9},
        {"name": "CIFAR-10", "entity_type": "Dataset", "description": "图像数据集",
         "aliases": [], "evidence_block_id": 2, "confidence": 0.99},
    ],
    "relations": [
        {"source": "THIS_PAPER", "target": "FedProx", "relation_type": "PROPOSES",
         "evidence_type": "explicit", "evidence_block_id": 1,
         "evidence_text": "FedProx is a federated optimization framework for "
                          "heterogeneous networks.", "confidence": 0.95},
        {"source": "FedProx", "target": "Statistical Heterogeneity",
         "relation_type": "MITIGATES", "evidence_type": "explicit",
         "evidence_block_id": 1,
         "evidence_text": "It addresses statistical heterogeneity in cross-device settings.",
         "confidence": 0.9},
        {"source": "THIS_PAPER", "target": "CIFAR-10", "relation_type": "USES",
         "evidence_type": "explicit", "evidence_block_id": 2,
         "evidence_text": "We evaluate FedProx on CIFAR-10 and MNIST",
         "confidence": 0.99},
    ],
}


@pytest.fixture()
def kg_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "library.db")
    app_db.init_db()
    return tmp_path / "library.db"


def seed_paper(pid="p1", blocks=BLOCKS, title="FedProx: A Federated Optimization Framework"):
    app_db.ex("INSERT INTO papers(id, title, status, created_at, updated_at) "
              "VALUES(?,?,?,?,?)", (pid, title, "ready", now(), now()))
    for i, t in enumerate(blocks):
        app_db.ex("INSERT INTO blocks(paper_id, order_idx, type, en) VALUES(?,?,?,?)",
                  (pid, i, "text", t))
    first = app_db.row("SELECT id FROM blocks WHERE paper_id=? ORDER BY order_idx LIMIT 1",
                       (pid,))
    app_db.ex("INSERT INTO sections(paper_id, title, title_en, block_start, order_idx, "
              "en_text) VALUES(?,?,?,?,?,?)",
              (pid, "Method", "Method", first["id"], 0, "\n".join(blocks)))
    return pid


def validate_good(pid="p1"):
    block_map = {i + 1: t for i, t in enumerate(BLOCKS)}
    return kg_schema.validate_extraction(dict(LLM_GOOD), block_map, "FedProx")


# ---------- Schema 校验 ----------

def test_validate_keeps_explicit_evidence_and_this_paper(kg_db):
    ents, rels, warns = validate_good()
    assert ents and not warns
    names = {e.name for e in ents}
    assert names == {"FedProx", "Statistical Heterogeneity", "CIFAR-10"}
    types = {(r.source, r.target, r.relation_type) for r in rels}
    assert ("THIS_PAPER", "FedProx", "PROPOSES") in types
    assert ("FedProx", "Statistical Heterogeneity", "MITIGATES") in types


def test_validate_drops_endpoint_type_violation(kg_db):
    data = {
        "entities": [
            {"name": "CIFAR-10", "entity_type": "Dataset", "aliases": [],
             "evidence_block_id": 2, "evidence_text": "We evaluate FedProx on CIFAR-10",
             "confidence": 0.99},
            {"name": "FedProx", "entity_type": "Algorithm", "aliases": [],
             "evidence_block_id": 1, "evidence_text": "FedProx is a federated",
             "confidence": 0.95},
        ],
        "relations": [
            # Dataset 不能作为 IMPROVES 的起点 → 必须丢弃
            {"source": "CIFAR-10", "target": "FedProx", "relation_type": "IMPROVES",
             "evidence_type": "explicit", "evidence_block_id": 2,
             "evidence_text": "We evaluate FedProx on CIFAR-10 and MNIST and report "
                              "accuracy improvements over FedAvg.", "confidence": 0.9},
            {"source": "THIS_PAPER", "target": "CIFAR-10", "relation_type": "USES",
             "evidence_type": "explicit", "evidence_block_id": 2,
             "evidence_text": "We evaluate FedProx on CIFAR-10 and MNIST",
             "confidence": 0.99},
        ],
    }
    block_map = {i + 1: t for i, t in enumerate(BLOCKS)}
    ents, rels, warns = kg_schema.validate_extraction(data, block_map, "")
    assert len(rels) == 1 and rels[0].relation_type == "USES"
    assert any("类型不符" in w or "IMPROVES" in w for w in warns)


def test_validate_drops_fabricated_evidence_and_unknown_entity(kg_db):
    data = {
        "entities": [
            {"name": "FedNova", "entity_type": "Algorithm", "aliases": [],
             "evidence_block_id": 1, "confidence": 0.9},  # 全文没有 FedNova
            {"name": "CIFAR-10", "entity_type": "Dataset", "aliases": [],
             "evidence_block_id": 2, "confidence": 0.9},
        ],
        "relations": [
            # 证据文本是模型编的（原文没有）
            {"source": "THIS_PAPER", "target": "CIFAR-10", "relation_type": "USES",
             "evidence_type": "explicit", "evidence_block_id": 2,
             "evidence_text": "CIFAR-10 significantly outperforms all baselines by 20%",
             "confidence": 0.9},
        ],
    }
    block_map = {i + 1: t for i, t in enumerate(BLOCKS)}
    ents, rels, warns = kg_schema.validate_extraction(data, block_map, "")
    # FedNova 全文不存在 → 丢弃；CIFAR-10 有真实证据 → 保留；编造证据的关系 → 丢弃
    assert [e.name for e in ents] == ["CIFAR-10"]
    assert rels == []
    assert any("FedNova" in w for w in warns)
    assert any("找不到" in w for w in warns)


def test_validate_recovers_evidence_to_another_block(kg_db):
    data = {"entities": [], "relations": [
        {"source": "THIS_PAPER", "target": "CIFAR-10", "relation_type": "USES",
         "evidence_type": "explicit", "evidence_block_id": 99,  # 指错块
         "evidence_text": "We evaluate FedProx on CIFAR-10 and MNIST",
         "confidence": 0.9},
        {"name": "CIFAR-10", "entity_type": "Dataset", "aliases": [],
         "evidence_block_id": 2, "confidence": 0.9},
    ], "entities": [
        {"name": "CIFAR-10", "entity_type": "Dataset", "aliases": [],
         "evidence_block_id": 2, "confidence": 0.9},
    ]}
    block_map = {i + 1: t for i, t in enumerate(BLOCKS)}
    ents, rels, warns = kg_schema.validate_extraction(data, block_map, "")
    assert len(rels) == 1 and rels[0].evidence_block_id == 2
    assert not any("找不到" in w for w in warns)


# ---------- 实体消歧 ----------

def _ent_rows():
    return app_db.rows("SELECT * FROM kg_entities ORDER BY canonical_name")


def test_resolver_exact_and_alias_reuse(kg_db):
    with app_db.db() as c:
        cache = {}
        e1 = kg_resolver.resolve_entity(c, cache, "FedProx", "Algorithm",
                                        aliases=["Federated Proximal"])
        e2 = kg_resolver.resolve_entity(c, cache, "fedprox", "Algorithm")
        # 别名已登记 → 命中同一实体
        e3 = kg_resolver.resolve_entity(c, cache, "Federated Proximal", "Algorithm")
        # 从未登记过的新名字，与单词名不模糊合并 → 应新建节点
        e4 = kg_resolver.resolve_entity(c, cache, "Federated Optimization Framework",
                                        "Algorithm")
    assert e1 == e2 == e3
    assert e4 != e1
    aliases = {a["alias"] for a in app_db.rows("SELECT alias FROM kg_entity_aliases")}
    assert "Federated Proximal" in aliases


def test_resolver_never_fuzzy_merges_single_word_names(kg_db):
    """CIFAR-10 与 CIFAR-100 名字高度相似，但绝不允许自动合并。"""
    with app_db.db() as c:
        cache = {}
        e1 = kg_resolver.resolve_entity(c, cache, "CIFAR-10", "Dataset")
        e2 = kg_resolver.resolve_entity(c, cache, "CIFAR-100", "Dataset")
    assert e1 != e2


def test_resolver_conservative_multiword_merge(kg_db):
    with app_db.db() as c:
        cache = {}
        e1 = kg_resolver.resolve_entity(c, cache, "Statistical Heterogeneity",
                                        "ResearchProblem")
        e2 = kg_resolver.resolve_entity(c, cache, "Statistical Heterogeneities",
                                        "ResearchProblem")
    assert e1 == e2


# ---------- 存储层：幂等替换 / 跨论文复用 ----------

def _store_good(pid="p1"):
    ents, rels, _ = validate_good(pid)
    return kg_store.replace_paper_extraction(pid, ents, rels)


def test_store_writes_entities_relations_evidence(kg_db):
    seed_paper()
    stats = _store_good()
    assert stats["entities"] == 3 and stats["relations"] == 3
    ov = kg_queries.overview()
    assert ov["entities_total"] == 4  # 3 抽取实体 + 本文论文节点
    assert ov["relations"]["PROPOSES"] == 1
    assert ov["papers_extracted"] == 1


def test_store_is_idempotent_on_repeated_extraction(kg_db):
    seed_paper()
    _store_good()
    _store_good()
    ov = kg_queries.overview()
    assert ov["entities_total"] == 4
    assert ov["evidence_total"] == 6  # 3 实体证据 + 3 关系证据，不重复


def test_store_replaces_stale_entities_on_reextraction(kg_db):
    seed_paper()
    _store_good()
    # 第二次抽取不再包含 Statistical Heterogeneity → 该实体应被清理
    ents, rels, _ = validate_good()
    ents2 = [e for e in ents if e.name != "Statistical Heterogeneity"]
    rels2 = [r for r in rels if r.target != "Statistical Heterogeneity"]
    kg_store.replace_paper_extraction("p1", ents2, rels2)
    names = {e["canonical_name"] for e in _ent_rows()}
    assert "Statistical Heterogeneity" not in names
    assert "CIFAR-10" in names


def test_store_shares_entities_across_papers(kg_db):
    """两篇论文提到同一实体 → 节点复用，关系挂两条证据。"""
    seed_paper("p1")
    seed_paper("p2", title="FedNova: Tackling Objective Inconsistency")
    for pid in ("p1", "p2"):
        block_map = {i + 1: t for i, t in enumerate(BLOCKS)}
        data = dict(LLM_GOOD)
        data["relations"] = [r for r in data["relations"]
                             if r["relation_type"] != "PROPOSES"]
        ents, rels, warns = kg_schema.validate_extraction(data, block_map, "")
        assert not warns
        kg_store.replace_paper_extraction(pid, ents, rels)
    cifar = app_db.row("SELECT id FROM kg_entities WHERE canonical_name='CIFAR-10'")
    assert app_db.row("SELECT COUNT(*) n FROM kg_evidence WHERE entity_id=?",
                      (cifar["id"],))["n"] == 2
    # THIS_PAPER 端点是各论文节点 → USES 是两条独立关系（各 1 条证据）
    uses = app_db.rows("SELECT id FROM kg_relations WHERE relation_type='USES' "
                       "AND target_entity_id=?", (cifar["id"],))
    assert len(uses) == 2
    # 算法间关系与论文无关 → 跨论文复用同一条边，挂两条证据
    fedprox = app_db.row("SELECT id FROM kg_entities WHERE canonical_name='FedProx'")
    sh = app_db.row("SELECT id FROM kg_entities WHERE "
                    "canonical_name='Statistical Heterogeneity'")
    mit = app_db.row("SELECT id FROM kg_relations WHERE relation_type='MITIGATES' "
                     "AND source_entity_id=? AND target_entity_id=?", (fedprox["id"], sh["id"]))
    assert mit, "MITIGATES 关系应跨论文复用"
    assert app_db.row("SELECT COUNT(*) n FROM kg_evidence WHERE relation_id=?",
                      (mit["id"],))["n"] == 2


def test_drop_paper_data_cleans_up(kg_db):
    seed_paper("p1")
    _store_good()
    kg_resolver.drop_paper_data("p1")
    ov = kg_queries.overview()
    assert ov["entities_total"] == 0 and ov["evidence_total"] == 0


# ---------- 任务生命周期 ----------

async def _fake_chat_good(info, messages, max_tokens=None):
    import json
    return json.dumps(LLM_GOOD, ensure_ascii=False)


async def _fake_chat_garbage(info, messages, max_tokens=None):
    return "我觉得这篇文章讲的是联邦学习，挺好的。"


def test_run_job_success_and_query_paths(kg_db, monkeypatch):
    seed_paper()
    monkeypatch.setattr(kg_extract, "chat", _fake_chat_good)
    job = kg_extract.create_job("p1")
    import asyncio
    asyncio.run(kg_extract.run_job(job["id"]))
    j = app_db.row("SELECT * FROM kg_extraction_jobs WHERE id=?", (job["id"],))
    assert j["status"] == "completed"
    import json as _json
    stats = _json.loads(j["stats_json"])
    assert stats["entities"] == 3 and stats["relations"] == 3
    # 查询路径：单篇知识结构 + 图谱 + 证据定位
    st = kg_queries.paper_structure("p1")
    assert st["proposals"] and st["proposals"][0]["name"] == "FedProx"
    assert st["datasets"][0]["name"] == "CIFAR-10"
    assert st["problems"][0]["name"] == "Statistical Heterogeneity"
    rid = st["proposals"][0]["relation_id"]
    rd = kg_queries.relation_detail(rid)
    assert rd["evidence"] and rd["evidence"][0]["stale"] is False
    g = kg_queries.graph(paper_id="p1")
    labels = {n["label"] for n in g["nodes"]}
    assert "FedProx" in labels and any(n["type"] == "Paper" for n in g["nodes"])
    assert g["edges"] and all(edge["evidence_text"] for edge in g["edges"])
    assert all(edge["evidence_paper_id"] == "p1" for edge in g["edges"])


def test_run_job_failure_keeps_old_graph(kg_db, monkeypatch):
    seed_paper()
    _store_good()  # 已有图谱
    monkeypatch.setattr(kg_extract, "chat", _fake_chat_garbage)
    job = kg_extract.create_job("p1")
    import asyncio
    asyncio.run(kg_extract.run_job(job["id"]))
    j = app_db.row("SELECT * FROM kg_extraction_jobs WHERE id=?", (job["id"],))
    assert j["status"] == "failed" and "保持不变" in j["error_message"]
    ov = kg_queries.overview()
    assert ov["entities_total"] == 4  # 旧图谱完好


def test_create_job_rejects_parallel_runs(kg_db):
    seed_paper()
    kg_extract.create_job("p1")
    with pytest.raises(RuntimeError):
        kg_extract.create_job("p1")


def test_rebuild_missing_skips_completed_papers(kg_db, monkeypatch):
    seed_paper()
    seed_paper("p2", title="Second Paper")
    monkeypatch.setattr(kg_extract, "chat", _fake_chat_good)
    kg_extract.create_job("p1")  # p1 有进行中任务 → rebuild 跳过它
    import asyncio
    job_ids = kg_extract.rebuild_missing()
    assert len(job_ids) == 1  # 只有 p2 缺抽取
    asyncio.run(kg_extract.run_job(job_ids[0]))
    # p2 已完成、p1 仍在进行 → 没有需要补的
    assert kg_extract.rebuild_missing() == []
    p1_completed = app_db.row(
        "SELECT COUNT(*) n FROM kg_extraction_jobs WHERE paper_id='p1' AND "
        "status='completed'")["n"]
    assert p1_completed == 0


def test_auto_extract_skips_mock_engine(kg_db):
    seed_paper()  # 默认 provider=mock
    import asyncio
    asyncio.run(kg_extract.auto_extract("p1"))
    assert app_db.row("SELECT COUNT(*) n FROM kg_extraction_jobs")["n"] == 0


# ---------- 查询层补充 ----------

def test_evidence_stale_after_reparse(kg_db):
    seed_paper()
    _store_good()
    old_block_ids = {b["id"] for b in
                     app_db.rows("SELECT id FROM blocks WHERE paper_id='p1'")}
    # 模拟重解析：块全部换血（新 id），旧 block_id 失效
    app_db.ex("DELETE FROM blocks WHERE paper_id='p1'")
    app_db.ex("INSERT INTO blocks(paper_id, order_idx, type, en) VALUES('p1',0,'text','new body')")
    st = kg_queries.paper_structure("p1")
    assert st["proposals"]
    assert st["proposals"][0]["block_id"] in old_block_ids  # 证据还指旧块
    rd = kg_queries.relation_detail(st["proposals"][0]["relation_id"])
    assert rd["evidence"] and rd["evidence"][0]["stale"] is True


def test_topics_fallback_to_shared_datasets(kg_db):
    """没有 ResearchArea 实体时，主题退化为"被 ≥2 篇使用的数据集"。"""
    for pid in ("p1", "p2"):
        seed_paper(pid, title=f"Paper {pid}")
        ents, rels, _ = validate_good(pid)
        kg_store.replace_paper_extraction(pid, ents, rels)
    t = kg_queries.topics()
    assert t["topics"] == []
    assert t["shared_resources"] and t["shared_resources"][0]["name"] == "CIFAR-10"
    assert t["shared_resources"][0]["paper_count"] == 2



def test_test_scope_paper_is_hidden_from_all_graph_queries(kg_db):
    for pid in ("normal-a", "normal-b", "test-paper"):
        seed_paper(pid, title=f"Paper {pid}")
        ents, rels, _ = validate_good(pid)
        kg_store.replace_paper_extraction(pid, ents, rels)
    app_db.ex("UPDATE papers SET library_role='test' WHERE id='test-paper'")

    overview = kg_queries.overview()
    assert overview["papers_total"] == 2
    assert overview["papers_extracted"] == 2
    assert overview["entities_total"] == 5
    assert kg_queries.paper_structure("test-paper") is None

    graph = kg_queries.graph()
    assert graph["nodes"]
    assert all("test-paper" not in n["id"] for n in graph["nodes"])
    assert all("test-paper" not in e["paper_ids"] for e in graph["edges"])


def test_excluded_paper_cannot_start_extraction(kg_db):
    seed_paper("test-paper")
    app_db.ex("UPDATE papers SET library_role='excluded' WHERE id='test-paper'")
    with pytest.raises(RuntimeError, match="测试/排除"):
        kg_extract.create_job("test-paper")


def test_library_role_migration_defaults_existing_papers_to_normal(kg_db, tmp_path, monkeypatch):
    import sqlite3
    legacy_db = tmp_path / "legacy-library.db"
    monkeypatch.setattr(app_db, "DB_PATH", legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE papers(id TEXT PRIMARY KEY, title TEXT, status TEXT)")
        conn.execute("INSERT INTO papers(id,title,status) VALUES('legacy-paper','Legacy','ready')")
    app_db.init_db()
    p = app_db.row("SELECT library_role FROM papers WHERE id='legacy-paper'")
    assert p["library_role"] == "normal"



def test_rebuild_missing_ignores_test_and_excluded_papers(kg_db):
    for pid, role in (("normal-paper", "normal"), ("test-paper", "test"),
                      ("excluded-paper", "excluded")):
        seed_paper(pid)
        app_db.ex("UPDATE papers SET library_role=? WHERE id=?", (role, pid))
    jobs = kg_extract.rebuild_missing()
    assert len(jobs) == 1
    assert app_db.row("SELECT paper_id FROM kg_extraction_jobs WHERE id=?", (jobs[0],))["paper_id"] == "normal-paper"


def test_test_only_evidence_cannot_upgrade_shared_relation(kg_db):
    for pid in ("normal-paper", "test-paper"):
        seed_paper(pid)
        ents, rels, _ = validate_good(pid)
        kg_store.replace_paper_extraction(pid, ents, rels)
    app_db.ex("UPDATE papers SET library_role='test' WHERE id='test-paper'")
    rel = app_db.row(
        "SELECT r.id FROM kg_relations r JOIN kg_evidence e ON e.relation_id=r.id "
        "WHERE e.paper_id='normal-paper' AND r.relation_type='MITIGATES' LIMIT 1")
    app_db.ex("UPDATE kg_evidence SET evidence_type='similarity', confidence=0.2 "
              "WHERE paper_id='normal-paper' AND relation_id=?", (rel["id"],))
    app_db.ex("UPDATE kg_relations SET evidence_type='explicit', confidence=0.99 WHERE id=?",
              (rel["id"],))
    visible = app_db.row("SELECT evidence_type, confidence FROM kg_visible_relations WHERE id=?",
                         (rel["id"],))
    assert visible["evidence_type"] == "similarity"
    assert visible["confidence"] == pytest.approx(0.2)



def test_test_paper_cannot_write_graph_even_if_storage_called_directly(kg_db):
    seed_paper("test-paper")
    app_db.ex("UPDATE papers SET library_role='test' WHERE id='test-paper'")
    ents, rels, _ = validate_good("test-paper")
    with pytest.raises(RuntimeError, match="不能写入"):
        kg_store.replace_paper_extraction("test-paper", ents, rels)


def test_run_job_marks_failed_when_persist_raises(kg_db, monkeypatch):
    """写库阶段抛错（如论文中途被删/改分类）时，任务必须落地为 failed，不能永远停在 running。"""
    seed_paper()
    monkeypatch.setattr(kg_extract, "chat", _fake_chat_good)
    from app.services import kg_store

    def _boom(pid, ents, rels):
        raise RuntimeError("论文不存在")
    monkeypatch.setattr(kg_store, "replace_paper_extraction", _boom)
    job = kg_extract.create_job("p1")
    import asyncio
    asyncio.run(kg_extract.run_job(job["id"]))
    j = app_db.row("SELECT * FROM kg_extraction_jobs WHERE id=?", (job["id"],))
    assert j["status"] == "failed"
    assert "写入图谱失败" in j["error_message"]


def test_graph_defaults_to_explicit_evidence_and_can_opt_in_to_inference(kg_db):
    seed_paper("inferred-only")
    ents, rels, _ = validate_good()
    inferred = [r.model_copy(update={"evidence_type": "inferred"}) for r in rels]
    kg_store.replace_paper_extraction("inferred-only", ents, inferred)

    safe_graph = kg_queries.graph(paper_id="inferred-only")
    exploratory_graph = kg_queries.graph(
        paper_id="inferred-only", evidence_types=["inferred"])

    assert safe_graph["nodes"]  # Nodes remain discoverable.
    assert safe_graph["edges"] == []  # Inference is not silently presented as fact.
    assert len(exploratory_graph["edges"]) == 3


def test_relation_trust_summary_recomputed_after_reextraction(kg_db):
    seed_paper("explicit-paper")
    seed_paper("inferred-paper")
    ents, rels, _ = validate_good()
    kg_store.replace_paper_extraction("explicit-paper", ents, rels)

    inferred_rels = [
        r.model_copy(update={
            "evidence_type": "inferred",
            "confidence": 0.7,
        }) for r in rels
    ]
    kg_store.replace_paper_extraction("inferred-paper", ents, inferred_rels)

    algorithm = app_db.row(
        "SELECT id FROM kg_entities WHERE canonical_name='FedProx'")
    problem = app_db.row(
        "SELECT id FROM kg_entities WHERE canonical_name='Statistical Heterogeneity'")
    relation = app_db.row(
        "SELECT * FROM kg_relations WHERE source_entity_id=? AND target_entity_id=? "
        "AND relation_type='MITIGATES'",
        (algorithm["id"], problem["id"]))
    assert relation["evidence_type"] == "explicit"
    app_db.ex("UPDATE kg_relations SET review_status='verified' WHERE id=?",
              (relation["id"],))

    # The explicit source is re-extracted and no longer asserts MITIGATES.
    kg_store.replace_paper_extraction(
        "explicit-paper", ents,
        [r for r in rels if r.relation_type != "MITIGATES"])

    relation = app_db.row("SELECT * FROM kg_relations WHERE id=?", (relation["id"],))
    assert relation["evidence_type"] == "inferred"
    assert relation["confidence"] == pytest.approx(0.7)
    assert relation["review_status"] == "unreviewed"


def test_schema_rejects_inferred_relation_even_when_quote_exists(kg_db):
    data = dict(LLM_GOOD)
    data["entities"] = list(LLM_GOOD["entities"])
    data["relations"] = [dict(item) for item in LLM_GOOD["relations"]]
    data["relations"][1]["evidence_type"] = "inferred"
    block_map = {i + 1: t for i, t in enumerate(BLOCKS)}

    entities, relations, warnings = kg_schema.validate_extraction(data, block_map, "")

    assert entities
    assert all(r.relation_type != "MITIGATES" for r in relations)
    assert any("不是原文明示" in warning for warning in warnings)


def test_rejected_relations_are_hidden_but_recoverable(kg_db):
    seed_paper("review-paper")
    _store_good("review-paper")
    relation = app_db.row(
        "SELECT id FROM kg_relations WHERE relation_type='MITIGATES'")
    app_db.ex("UPDATE kg_relations SET review_status='rejected' WHERE id=?",
              (relation["id"],))

    normal = kg_queries.graph(paper_id="review-paper")
    with_rejected = kg_queries.graph(
        paper_id="review-paper", include_rejected=True)

    assert len(normal["edges"]) == 2
    assert len(with_rejected["edges"]) == 3
    rejected = next(e for e in with_rejected["edges"] if e["id"] == relation["id"])
    assert rejected["review_status"] == "rejected"


def test_relation_review_state_migrates_as_unreviewed(kg_db):
    relation_cols = {
        r["name"] for r in app_db.rows("PRAGMA table_info(kg_relations)")
    }
    assert "review_status" in relation_cols


def test_overview_reports_manual_review_coverage(kg_db):
    seed_paper("review-counts")
    _store_good("review-counts")
    relation_ids = app_db.rows("SELECT id FROM kg_relations ORDER BY id")
    app_db.ex("UPDATE kg_relations SET review_status='verified' WHERE id=?",
              (relation_ids[0]["id"],))
    app_db.ex("UPDATE kg_relations SET review_status='rejected' WHERE id=?",
              (relation_ids[1]["id"],))

    review = kg_queries.overview()["relation_review"]
    assert review["verified"] == 1
    assert review["rejected"] == 1
    assert review["unreviewed"] == 1


def test_focus_graph_is_bounded_to_one_hop_neighbours(kg_db):
    seed_paper("focus-paper")
    _store_good("focus-paper")
    algorithm = app_db.row(
        "SELECT id FROM kg_entities WHERE canonical_name='FedProx'")
    focused = kg_queries.graph(focus_entity_id=algorithm["id"])

    node_ids = {node["id"] for node in focused["nodes"]}
    assert algorithm["id"] in node_ids
    assert len(node_ids) <= 31
    assert focused["edges"]
    assert kg_queries.graph(focus_entity_id="missing-entity")["nodes"] == []
