"""V3.1 chunking, theory-logic and coverage contract tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import db as app_db
from app.services import kg_understanding_v31 as v31
import pytest


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "library.db")
    app_db.init_db()
    return tmp_path / "library.db"


def test_long_section_is_chunked_without_the_old_5200_character_cap(isolated_db):
    text = ("A complete paragraph about the formal definition and its proof. " * 240)
    block_id = app_db.ex(
        "INSERT INTO blocks(paper_id,order_idx,type,en) VALUES(?,?,?,?)",
        ("paper-long", 0, "text", text),
    )
    app_db.ex(
        "INSERT INTO sections(paper_id,title,title_en,block_start,order_idx,skip) VALUES(?,?,?,?,?,?)",
        ("paper-long", "Constitutive Definition", "Constitutive Definition", block_id, 0, 0),
    )
    sections, block_map = v31._load_sections("paper-long")
    chunks = v31._make_chunks(sections)
    assert len(block_map[block_id]) == len(text.strip())
    assert len(chunks) > 1
    assert all(segment["block_id"] == block_id for chunk in chunks for segment in chunk["segments"])
    joined = "".join(segment["text"] for chunk in chunks for segment in chunk["segments"])
    assert len(joined) >= len(text) - 20


def test_chunking_preserves_multiple_sections_and_pre_section_content(isolated_db):
    first = app_db.ex(
        "INSERT INTO blocks(paper_id,order_idx,type,en) VALUES(?,?,?,?)",
        ("paper-sections", 0, "text", "An abstract paragraph with enough text to be included in analysis."),
    )
    second = app_db.ex(
        "INSERT INTO blocks(paper_id,order_idx,type,en) VALUES(?,?,?,?)",
        ("paper-sections", 1, "text", "A theorem statement with enough text to be included in analysis."),
    )
    app_db.ex(
        "INSERT INTO sections(paper_id,title,title_en,block_start,order_idx,skip) VALUES(?,?,?,?,?,?)",
        ("paper-sections", "Theory", "Theory", second, 0, 0),
    )
    sections, block_map = v31._load_sections("paper-sections")
    assert len(sections) == 2
    assert first in block_map and second in block_map


def test_joint_sufficiency_requires_multiple_premises_and_exact_evidence(isolated_db):
    payload = {
        "contributions": [{"id": "h"}, {"id": "t1"}, {"id": "t2"}],
        "theory_logic": [
            {"id": "ok", "relation_type": "JOINTLY_SUFFICIENT_FOR",
             "source_ids": ["t1", "t2"], "target_id": "h",
             "statement": "The two conditions jointly define the concept.",
             "evidence": {"block_id": 1, "quote": "Together, these two conditions are sufficient for H."}},
            {"id": "bad", "relation_type": "JOINTLY_SUFFICIENT_FOR",
             "source_ids": ["t1"], "target_id": "h",
             "statement": "One condition is sufficient.",
             "evidence": {"block_id": 1, "quote": "Together, these two conditions are sufficient for H."}},
        ],
    }
    warnings = v31._validate_logic(
        payload, {1: "Together, these two conditions are sufficient for H."}
    )
    assert [x["id"] for x in payload["theory_logic"]] == ["ok"]
    assert payload["theory_logic"][0]["verification"] == "evidence_supported"
    assert warnings


def test_logic_validator_rejects_non_verbatim_or_unknown_endpoints(isolated_db):
    payload = {
        "contributions": [{"id": "a"}, {"id": "b"}],
        "theory_logic": [{
            "relation_type": "HAS_NECESSARY_CONDITION", "source_ids": ["missing"],
            "target_id": "b", "statement": "invalid", "evidence": {
                "block_id": 9, "quote": "This is not in the supplied source text."
            }
        }],
    }
    warnings = v31._validate_logic(payload, {1: "Some actual source evidence is present."})
    assert payload["theory_logic"] == []
    assert warnings
