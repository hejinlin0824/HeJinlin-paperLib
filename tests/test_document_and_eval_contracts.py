"""Fast offline contracts: synthetic MinerU payloads only, no PDFs, DB or network."""
import json
from pathlib import Path
import pytest
from app.services.md2blocks import from_content_list, from_mineru_dir
from app.services.paper_document_contract import normalize_mineru_document, validate_mineru_document
from app.services.structured_output import StructuredOutputValidationError, chunk_analysis_schema, parse_and_validate
from app.services.ragas_eval import load_eval_fixture, run_ragas

ROOT = Path(__file__).resolve().parent

def test_synthetic_mineru_content_list_preserves_semantic_block_types_and_provenance(tmp_path):
    raw = json.loads((ROOT / "fixtures/synthetic_mineru_content_list.json").read_text(encoding="utf-8"))
    blocks = from_content_list(raw, None, tmp_path)
    doc = normalize_mineru_document(blocks)
    assert validate_mineru_document(doc) == []
    kinds = {item["type"] for item in doc["blocks"]}
    assert {"heading", "text", "formula", "table", "image", "references"} <= kinds
    formula = next(item for item in doc["blocks"] if item["type"] == "formula")
    table = next(item for item in doc["blocks"] if item["type"] == "table")
    assert formula["meta"]["latex"] == "x^2 + y^2 = z^2"
    assert table["meta"]["table_html"].startswith("<table>")
    assert all(item["meta"]["source"]["engine"] == "mineru" for item in doc["blocks"])
    assert any(item["meta"].get("mineru_source_index") == 2 for item in doc["blocks"])

def test_document_contract_rejects_missing_table_body_and_duplicate_order():
    doc = normalize_mineru_document([{"type":"table","en":"","meta":{},"order_idx":0},
                                     {"type":"text","en":"body","meta":{},"order_idx":0}])
    errors = validate_mineru_document(doc)
    assert any("table body" in item for item in errors)
    assert any("unique integer" in item for item in errors)

def test_schema_constrained_output_accepts_valid_chunk_and_rejects_invalid_json():
    schema = chunk_analysis_schema()
    valid = {"section_role":"theory","summary":"A definition.",
             "claims":[{"claim":"A widget is deterministic.","kind":"definition",
                        "evidence":[{"block_id":1,"quote":"A widget is deterministic."}]}],
             "argument_steps":[],"candidate_contributions":[],"open_questions":[]}
    assert parse_and_validate(json.dumps(valid), schema) == valid
    with pytest.raises(StructuredOutputValidationError, match="required"):
        parse_and_validate('{"summary":"missing required fields"}', schema)
    with pytest.raises(StructuredOutputValidationError, match="invalid_json"):
        parse_and_validate("{not json", schema)

def test_ragas_fixture_is_small_and_validated_without_running_model():
    fixture = load_eval_fixture(str(ROOT / "fixtures/synthetic_ragas_v1.json"))
    assert len(fixture["questions"]) == 2 and len(fixture["contexts"]) == 2
    assert all(isinstance(context,list) and context for context in fixture["contexts"])
    # No Ragas evaluator is constructed here: the unit tier must never call a model or network.
    assert len(fixture["ground_truths"]) == 2
    result = run_ragas(fixture)
    assert result["status"] == "configuration_required"

def test_synthetic_quality_fixture_is_explicitly_not_a_real_paper():
    fixture = json.loads((ROOT / "fixtures/synthetic_paper_gold_v1.json").read_text(encoding="utf-8"))
    assert fixture["fixture_id"] == "synthetic-paper-v1"
    assert fixture["paper_type"] == "theory" and fixture["expected_negative_cases"]

def test_strict_schema_response_format_is_constructed():
    from app.services.structured_output import response_format
    value = response_format("synthetic", {"type":"object","properties":{},"required":[],"additionalProperties":False})
    assert value["type"] == "json_schema" and value["json_schema"]["strict"] is True

def test_schema_probe_is_explicit_and_reports_mock_provider(monkeypatch):
    from app.services import kg_understanding_v4 as v4
    monkeypatch.setattr(v4,"engine_info",lambda:{"provider":"mock","model":"mock"})
    monkeypatch.setattr(v4,"engine_ready",lambda _info:(True,""))
    async def fake_chat(*_args,**kwargs):
        assert kwargs["response_format"]["type"] == "json_schema"
        return '{"ok": true}'
    monkeypatch.setattr(v4,"chat",fake_chat)
    import asyncio
    result = asyncio.run(v4.probe_structured_output())
    assert result["status"] == "supported" and result["mode"] == "json_schema"



def test_v4_chat_falls_back_only_when_provider_rejects_schema(monkeypatch):
    from app.services import kg_understanding_v4 as v4
    calls = []
    async def fake_chat(_info, _messages, **kwargs):
        calls.append(kwargs.get("response_format"))
        if len(calls) == 1:
            raise RuntimeError("HTTP 400: unsupported response_format json_schema")
        if len(calls) == 2:
            raise RuntimeError("HTTP 400: response_format json_object unsupported")
        return '{"ok": true}'
    monkeypatch.setattr(v4, "chat", fake_chat)
    import asyncio
    result = asyncio.run(v4._chat_v4({"provider":"custom"}, [], 64,
        schema_name="probe", schema={"type":"object","properties":{"ok":{"type":"boolean"}},
        "required":["ok"],"additionalProperties":False}))
    assert result == '{"ok": true}'
    assert calls[0]["type"] == "json_schema"
    assert calls[1] == {"type":"json_object"}
    assert calls[2] is None


def test_translate_chat_includes_response_format_without_network(monkeypatch):
    from app.services import translate
    captured = {}
    class FakeResponse:
        status_code = 200
        text = ""
        def raise_for_status(self): pass
        def json(self):
            return {"choices":[{"message":{"content":"{\"ok\":true}"}}]}
    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def post(self, _url, headers=None, json=None):
            captured.update(json or {})
            return FakeResponse()
    monkeypatch.setattr(translate.httpx, "AsyncClient", FakeClient)
    import asyncio
    response_spec = {"type":"json_schema","json_schema":{"name":"probe","strict":True,
        "schema":{"type":"object","properties":{"ok":{"type":"boolean"}},
                  "required":["ok"],"additionalProperties":False}}}
    result = asyncio.run(translate.chat(
        {"provider":"custom","model":"mock-model","base_url":"http://localhost/v1",
         "api_key":"test","temperature":0}, [{"role":"user","content":"test"}],
        max_tokens=64, retries=1, response_format=response_spec))
    assert result == '{"ok":true}'
    assert captured["response_format"] == response_spec



def test_mineru_directory_adapter_returns_contract_metadata(tmp_path):
    extract_dir = tmp_path / "mineru-result"
    extract_dir.mkdir()
    raw = json.loads((ROOT / "fixtures/synthetic_mineru_content_list.json").read_text(encoding="utf-8"))
    (extract_dir / "synthetic_content_list.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    result = from_mineru_dir(extract_dir, tmp_path / "work")
    assert result["meta"]["document_contract"]["schema_version"] == "paper-document/1.0"
    assert result["meta"]["document_validation_errors"] == []
    assert result["meta"]["document_contract"]["source_engine"] == "mineru"


def test_mineru_markdown_fallback_is_explicitly_labeled(tmp_path):
    extract_dir = tmp_path / "mineru-markdown"
    extract_dir.mkdir()
    (extract_dir / "paper.md").write_text("# Synthetic title\n\nA synthetic paragraph for fallback parsing.\n", encoding="utf-8")
    result = from_mineru_dir(extract_dir, tmp_path / "work")
    assert result["meta"]["document_contract"]["source_engine"] == "mineru_markdown_fallback"
    assert result["meta"]["document_validation_errors"] == []
