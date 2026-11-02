"""V3 paper-understanding contract tests; no model calls or production DB writes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import pytest
from app import db as app_db
from app.services import kg_understanding as v3
from app.util import now

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "library.db")
    app_db.init_db()
    v3.ensure_tables()
    return tmp_path / "library.db"

def test_v3_evidence_requires_exact_quote_and_real_block(isolated_db):
    payload = {
        "contributions": [
            {"id":"c1","title":"Four necessary and sufficient conditions","importance":"core",
             "evidence":[{"block_id":1,"quote":"The four conditions are necessary and sufficient."}]},
            {"id":"c2","title":"Invented contribution","importance":"core",
             "evidence":[{"block_id":99,"quote":"This quote is not in the source text."}]},
        ],
        "theoretical_framework": [], "method_architecture": [], "key_findings": []
    }
    warnings = v3._validate_evidence(payload, {1:"The four conditions are necessary and sufficient for convergence."})
    assert [x["id"] for x in payload["contributions"]] == ["c1"]
    assert len(payload["contributions"][0]["evidence"]) == 1
    assert warnings

def test_v3_relation_only_uses_contribution_tree_nodes_and_exact_evidence(isolated_db):
    payload = {
        "contributions":[
            {"id":"c1","title":"Condition A"},
            {"id":"c2","title":"Theorem B"},
        ],
        "relations":[
            {"source_id":"c1","target_id":"c2","relation_type":"NECESSARY_FOR",
             "evidence":{"block_id":2,"quote":"Condition A is necessary for Theorem B."}},
            {"source_id":"c1","target_id":"not-a-node","relation_type":"IMPROVES",
             "evidence":{"block_id":2,"quote":"Condition A is necessary for Theorem B."}},
            {"source_id":"c1","target_id":"c2","relation_type":"IMPROVES",
             "evidence":{"block_id":2,"quote":"The methods were evaluated on CIFAR-10."}},
        ]
    }
    warnings = v3._normalise_relations(payload, {2:"Condition A is necessary for Theorem B. The methods were evaluated on CIFAR-10."})
    assert len(payload["relations"]) == 1
    assert payload["relations"][0]["verification"] == "unverified"
    assert warnings

def test_v3_draft_table_is_isolated_from_legacy_graph_tables(isolated_db):
    app_db.ex("INSERT INTO papers(id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
              ("paper-v3","A theory paper","ready",now(),now()))
    app_db.ex("INSERT INTO blocks(paper_id,order_idx,type,en) VALUES(?,?,?,?)",
              ("paper-v3",0,"text","A sufficiently long source paragraph for a paper understanding test."))
    draft = v3.create_draft("paper-v3")
    assert draft["status"] == "running"
    assert app_db.row("SELECT COUNT(*) n FROM kg_entities")["n"] == 0
    assert app_db.row("SELECT COUNT(*) n FROM kg_relations")["n"] == 0
    assert app_db.row("SELECT COUNT(*) n FROM kg_understanding_drafts")["n"] == 1

def test_v3_review_status_does_not_write_legacy_graph(isolated_db):
    app_db.ex("INSERT INTO papers(id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
              ("paper-v3","A theory paper","ready",now(),now()))
    app_db.ex("INSERT INTO blocks(paper_id,order_idx,type,en) VALUES(?,?,?,?)",
              ("paper-v3",0,"text","A sufficiently long source paragraph for a paper understanding test."))
    draft = v3.create_draft("paper-v3")
    app_db.ex("UPDATE kg_understanding_drafts SET status='draft' WHERE id=?",(draft["id"],))
    payload = {"paper_type":"theory","contributions":[],"relations":[]}
    saved = v3.update_review(draft["id"],payload,"approved")
    assert saved["status"] == "approved"
    assert app_db.row("SELECT COUNT(*) n FROM kg_entities")["n"] == 0
    assert app_db.row("SELECT COUNT(*) n FROM kg_relations")["n"] == 0
