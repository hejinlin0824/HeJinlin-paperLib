"""V4 retrieval, rule NLP and fallback contracts; no model/network calls."""
import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import pytest
from app import db as app_db
from app.services import paper_intelligence_v4 as intel
from app.services import kg_understanding_v4 as v4
from app.services import kg_understanding_v31 as v31
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
    first, first_stats = v4._compact_notes([note])
    second, second_stats = v4._compact_notes([note])
    assert first == second
    assert first_stats == second_stats
    assert '"block_id":9' in first
    assert '"T1 is necessary."' in first
    assert first_stats["chunks_included"] == 1


def test_new_v4_draft_is_versioned_and_old_graph_remains_untouched(isolated_db):
    _paper_with_blocks()
    old_draft = v31.create_draft("harness-paper")
    app_db.ex("UPDATE kg_understanding_drafts SET status=? WHERE id=?", ("draft", old_draft["id"]))
    draft = v4.create_draft("harness-paper")
    assert draft["version"] == "4.0"
    assert v4.get_draft(draft["id"])["version"] == "4.0"
    assert v4.get_draft(old_draft["id"]) is None
    assert [item["id"] for item in v4.list_drafts("harness-paper")] == [draft["id"]]
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


def test_quality_evaluator_reports_proxy_metrics_and_evidence_accuracy():
    from app.services.paper_quality_eval import evaluate_payload
    gold = {"expected_concepts": ["agent loop", "joint sufficiency"],
            "expected_negative_cases": ["A UI is not necessary"]}
    payload = {
        "contributions": [{"title": "Agent loop", "evidence": [{"block_id": 2, "quote": "Agent loop is required."}]}],
        "theory_logic": [{"relation_type": "JOINTLY_SUFFICIENT_FOR", "source_ids": ["a", "b"],
                          "target_id": "h", "verification": "unverified",
                          "evidence": {"block_id": 2, "quote": "Agent loop is required."}}],
        "relations": [],
    }
    report = evaluate_payload(payload, gold, {2: "Agent loop is required."})
    assert report["evidence_exact_match_rate"] == 1.0
    assert report["joint_sufficiency_hyperedge_shape_rate"] == 1.0
    assert report["unverified_edge_count"] == 1
    assert report["negative_cases_for_human_review"] == ["A UI is not necessary"]

def test_default_search_is_lexical_when_no_local_vectors_are_configured(isolated_db, monkeypatch):
    _paper_with_blocks()
    monkeypatch.delenv("PAPERLIB_BGE_M3_PATH", raising=False)
    intel.index_paper("harness-paper", include_embeddings=False)
    result = intel.search_blocks("agent harness", "harness-paper")
    assert result["mode"] == "lexical"


def test_reindex_removes_stale_block_rows(isolated_db):
    first, second = _paper_with_blocks()
    intel.index_paper("harness-paper", include_embeddings=False)
    app_db.ex("DELETE FROM blocks WHERE id=?", (second,))
    refreshed = intel.index_paper("harness-paper", include_embeddings=False)
    assert refreshed["indexed_blocks"] == 1
    assert app_db.row("SELECT COUNT(*) n FROM paper_intelligence_chunks WHERE paper_id=?",
                      ("harness-paper",))["n"] == 1

def test_successful_v4_pipeline_reports_real_input_coverage(isolated_db, monkeypatch):
    first, _ = _paper_with_blocks()
    draft = v4.create_draft("harness-paper")
    monkeypatch.setattr(v4, "engine_info", lambda: {"provider": "custom", "model": "unit-test", "concurrency": 2})
    monkeypatch.setattr(v4, "engine_ready", lambda _info: (True, ""))
    chunk_response = {
        "section_role": "theory", "summary": "The paper defines an agent harness.",
        "claims": [{"claim": "An agent harness is a runtime engineering layer.",
                    "kind": "definition",
                    "evidence": [{"block_id": first,
                                  "quote": "Definition 1. An agent harness is a runtime engineering layer."}]}],
        "argument_steps": [], "candidate_contributions": [], "open_questions": []
    }
    synthesis_response = {
        "paper_type": "theory", "problem": "Terminological ambiguity", "thesis": "A constitutive definition",
        "contributions": [{"id": "harness-definition", "title": "Agent harness definition",
                           "description": "A runtime engineering layer.", "kind": "definition",
                           "importance": "core", "knowledge_role": "formal_definition", "parent_id": None,
                           "evidence": [{"block_id": first,
                                         "quote": "Definition 1. An agent harness is a runtime engineering layer."}]}],
        "theoretical_framework": [], "method_architecture": [], "key_findings": [],
        "limitations": [], "uncertainties": [], "relations": [], "theory_logic": [], "argument_map": []
    }
    responses = iter([json.dumps(chunk_response), json.dumps(synthesis_response)])
    async def fake_chat(*_args, **_kwargs):
        return next(responses)
    monkeypatch.setattr(v4, "_chat_v4", fake_chat)
    asyncio.run(v4.run_understanding(draft["id"]))
    saved = v4.get_draft(draft["id"])
    coverage = saved["payload"]["coverage"]
    assert saved["status"] == "draft"
    assert saved["payload"]["contributions"][0]["id"] == "harness-definition"
    assert coverage["completed_chunks"] == coverage["chunk_count"] == 1
    assert coverage["processed_chars"] == coverage["chunked_chars"]
    assert coverage["input_coverage"] == 1.0

def test_graph_context_retrieves_only_approved_v4_nodes_and_hyperedges(isolated_db):
    first, _ = _paper_with_blocks()
    draft = v4.create_draft("harness-paper")
    app_db.ex("UPDATE kg_understanding_drafts SET status=? WHERE id=?", ("draft", draft["id"]))
    payload = {
        "version": "4.0",
        "contributions": [
            {"id": "harness", "title": "Agent harness", "description": "Runtime engineering layer",
             "evidence": [{"block_id": first, "quote": "Definition 1. An agent harness is a runtime engineering layer."}]},
            {"id": "loop", "title": "Agent loop", "description": "Adaptive execution loop",
             "evidence": [{"block_id": first, "quote": "Definition 1. An agent harness is a runtime engineering layer."}]},
            {"id": "tools", "title": "Tool interface", "description": "External environment tools",
             "evidence": [{"block_id": first, "quote": "Definition 1. An agent harness is a runtime engineering layer."}]},
        ],
        "relations": [],
        "theory_logic": [{"id": "joint", "relation_type": "JOINTLY_SUFFICIENT_FOR",
                          "source_ids": ["loop", "tools"], "target_id": "harness",
                          "statement": "Loop and tools jointly support the harness definition.", "verification": "entailed",
                          "evidence": {"block_id": first, "quote": "Definition 1. An agent harness is a runtime engineering layer."}}],
    }
    assert intel.search_approved_graph("agent harness", "harness-paper")["results"] == []
    v4.update_review(draft["id"], payload, "approved")
    result = intel.search_approved_graph("agent harness", "harness-paper")
    assert result["results"]
    group = result["results"][0]
    assert any(node["id"] == "harness" for node in group["matched_nodes"])
    assert any(edge["kind"] == "theory_hyperedge" for edge in group["graph_paths"])


def test_chunk_batching_reduces_calls_and_preserves_per_chunk_sections():
    chunks = [
        {"section": "Method", "section_index": 0, "chunk_index": 0,
         "segments": [{"block_id": 1, "text": "First short chunk."}]},
        {"section": "Experiments", "section_index": 1, "chunk_index": 0,
         "segments": [{"block_id": 2, "text": "Second short chunk."}]},
        {"section": "Discussion", "section_index": 2, "chunk_index": 0,
         "segments": [{"block_id": 3, "text": "A different section."}]},
    ]
    batches = v4._build_chunk_batches(chunks)
    assert batches == [[0, 1], [2]]
    response = json.dumps({"chunks": [
        {"chunk_id": 1, "section_role": "method", "summary": "Second summary.",
         "claims": [], "argument_steps": [], "candidate_contributions": [], "open_questions": []},
        {"chunk_id": 0, "section_role": "method", "summary": "First summary.",
         "claims": [], "argument_steps": [], "candidate_contributions": [], "open_questions": []},
    ]})
    notes = v4._normalise_batch_notes(response, [0, 1], chunks)
    assert set(notes) == {0, 1}
    assert notes[0]["chunk_index"] == notes[1]["chunk_index"] == 0
    assert notes[0]["section_index"] == 0 and notes[1]["section_index"] == 1
    assert notes[0]["title"] == "Method"
    assert notes[1]["title"] == "Experiments"


def test_batch_parser_rejects_unknown_duplicate_and_empty_chunks():
    chunks = [{"section": "Method", "section_index": 0, "chunk_index": 0, "segments": []}]
    response = json.dumps({"chunks": [
        {"chunk_id": 0, "summary": "Valid"},
        {"chunk_id": 0, "summary": "Duplicate"},
        {"chunk_id": 9, "summary": "Unknown"},
        {"chunk_id": 0, "summary": ""},
    ]})
    notes = v4._normalise_batch_notes(response, [0], chunks)
    assert list(notes) == [0]
    assert notes[0]["summary"] == "Valid"


def test_v4_synthesis_compaction_is_bounded_and_keeps_every_chunk():
    notes = [{
        "title": f"Section {i // 3}", "section_index": i // 3, "chunk_index": i % 3,
        "section_role": "method", "summary": (f"Summary for chunk {i}. " + "detail " * 90),
        "claims": [{"claim": f"Claim {i}", "kind": "condition",
                   "evidence": [{"block_id": i + 1, "quote": f"Exact evidence quote for block {i + 1}."}]}],
        "argument_steps": ["Reasoning step " * 20],
        "candidate_contributions": ["Candidate " * 20], "open_questions": ["Question " * 20],
    } for i in range(24)]
    compact, stats = v4._compact_notes(notes, max_chars=6000)
    decoded = json.loads(compact)
    assert len(decoded) == len(notes)
    assert len(compact) <= 6000
    assert stats["chunks_included"] == stats["chunks_total"] == 24
    assert stats["truncated"] is True
    assert all(item["section_index"] == i // 3 for i, item in enumerate(decoded))


def test_v4_uses_shared_primitives_without_importing_v31_runtime():
    from app.services import paper_understanding_common as common
    assert v4._load_sections is common._load_sections
    assert v4._make_chunks is common._make_chunks
    assert v4._chunk_prompt is common._chunk_prompt
    assert v31._load_sections is common._load_sections
    assert v31._make_chunks is common._make_chunks
    assert "kg_understanding_v31" not in v4.__dict__
