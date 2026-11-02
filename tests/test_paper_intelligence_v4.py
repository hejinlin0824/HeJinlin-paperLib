"""V4 retrieval, rule NLP and fallback contracts; no model/network calls."""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import pytest
from app import db as app_db
from app.services import paper_intelligence_v4 as intel
from app.services import kg_understanding_v4 as v4
from app.util import now


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "library.db")
    app_db.init_db()
    return tmp_path / "library.db"


def _paper_with_blocks():
    app_db.ex(
        "INSERT INTO papers(id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
        ("harness-paper", "What Makes an Agent Harness?", "ready", now(), now()),
    )
    b1 = app_db.ex(
        "INSERT INTO blocks(paper_id,order_idx,type,en) VALUES(?,?,?,?)",
        ("harness-paper", 0, "text",
         "Definition 1. An agent harness is a runtime engineering layer. "
         "The system is a harness if and only if four conditions are satisfied: "
         "agent loop, tool interface, context management, and control mechanisms."),
    )
    b2 = app_db.ex(
        "INSERT INTO blocks(paper_id,order_idx,type,en) VALUES(?,?,?,?)",
        ("harness-paper", 1, "text",
         "The four conditions are jointly sufficient and each condition is necessary. "
         "We prove this claim in the following proposition."),
    )
    app_db.ex(
        "INSERT INTO sections(paper_id,title,title_en,block_start,order_idx,skip) VALUES(?,?,?,?,?,?)",
        ("harness-paper", "Constitutive Definition", "Constitutive Definition", b1, 0, 0),
    )
    return b1, b2


def test_tokenizer_handles_english_and_chinese_terms():
    tokens = intel.tokenize("Agent harness 的必要条件")
    assert "agent" in tokens and "harness" in tokens
    assert "必要" in tokens and "条件" in tokens


def test_local_index_is_idempotent_and_search_returns_source_block(isolated_db):
    b1, _ = _paper_with_blocks()
    first = intel.index_paper("harness-paper", include_embeddings=False)
    second = intel.index_paper("harness-paper", include_embeddings=False)
    assert first["indexed_blocks"] == second["indexed_blocks"] == 2
    result = intel.search_blocks("necessary sufficient harness", "harness-paper", top_k=5, dense=False)
    assert result["mode"] == "lexical"
    assert result["results"]
    assert any(item["block_id"] == b1 for item in result["results"])
    assert intel.index_status("harness-paper")["index_status"] == "lexical"


def test_rule_candidates_are_grounded_in_real_block_ids(isolated_db):
    b1, b2 = _paper_with_blocks()
    result = intel.extract_structure_candidates("harness-paper")
    assert result["candidate_count"] >= 3
    assert {x["block_id"] for x in result["candidates"]}.issubset({b1, b2})
    assert {x["kind"] for x in result["candidates"]} >= {"definition", "condition", "proof"}


def test_dense_model_is_opt_in_and_never_auto_downloaded(monkeypatch):
    monkeypatch.setenv("PAPERLIB_BGE_M3_PATH", "Z:/definitely-not-a-local-model")
    with pytest.raises(RuntimeError, match="不会自动联网下载"):
        intel._load_embedding_model()


def test_v4_compaction_is_deterministic_and_preserves_verbatim_evidence():
    note = {"title": "Theory", "section_role": "theory", "summary": "A claim",
            "claims": [{"claim": "T1 is necessary", "kind": "condition",
                        "evidence": [{"block_id": 9, "quote": "T1 is necessary."}]}],
            "argument_steps": ["The author removes T1 and shows failure."]}
    first = v4._compact_notes([note])
    second = v4._compact_notes([note])
    assert first == second
    assert '"block_id": 9' in first
    assert '"T1 is necessary."' in first


def test_new_v4_draft_is_versioned_and_old_graph_remains_untouched(isolated_db):
    _paper_with_blocks()
    draft = v4.create_draft("harness-paper")
    assert draft["version"] == "4.0"
    assert v4.get_draft(draft["id"])["version"] == "4.0"
    assert app_db.row("SELECT COUNT(*) n FROM kg_entities")["n"] == 0
    assert app_db.row("SELECT COUNT(*) n FROM kg_relations")["n"] == 0


def test_fallback_payload_never_labels_rule_candidates_as_verified_contributions():
    candidates = [{"kind": "condition", "matched_term": "necessary condition",
                   "block_id": 12, "excerpt": "This is a necessary condition for the definition."}]
    payload = v4._fallback_payload("Harness", [], candidates)
    assert payload["contributions"][0]["origin"] == "rule_candidate_not_model_verified"
    assert payload["contributions"][0]["importance"] == "supporting"
    assert payload["contributions"][0]["evidence"][0]["block_id"] == 12

def test_llm_chunk_failure_keeps_a_recoverable_v4_draft(isolated_db, monkeypatch):
    _paper_with_blocks()
    draft = v4.create_draft("harness-paper")
    monkeypatch.setattr(v4, "engine_info", lambda: {"provider": "custom", "model": "unit-test"})
    monkeypatch.setattr(v4, "engine_ready", lambda _info: (True, ""))
    async def failing_chat(*_args, **_kwargs):
        raise RuntimeError("simulated model timeout")
    monkeypatch.setattr(v4, "chat", failing_chat)
    asyncio.run(v4.run_understanding(draft["id"]))
    saved = v4.get_draft(draft["id"])
    assert saved["status"] == "draft"
    assert saved["payload"]["version"] == "4.0"
    assert saved["payload"]["coverage"]["failed_chunks"] >= 1
    assert any("分块" in warning for warning in saved["warnings"])
