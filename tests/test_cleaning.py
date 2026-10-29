import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.cleaning import (  # noqa: E402
    clean_blocks,
    clean_extracted_text,
    is_standalone_footnote,
    new_cleaning_report,
)


def test_standalone_arxiv_watermark_is_removed_but_real_body_survives():
    text = (
        "arXiv:2401.01234v2 [cs.CV] 2 Feb 2024\n"
        "We evaluate our method on three public benchmarks."
    )
    cleaned, removed = clean_extracted_text(text)
    assert cleaned == "We evaluate our method on three public benchmarks."
    assert [rule for rule, _ in removed] == ["arxiv_watermark"]


def test_body_sentence_that_mentions_arxiv_is_preserved():
    text = "Our baseline follows arXiv:2401.01234 and is compared with prior work."
    cleaned, removed = clean_extracted_text(text)
    assert cleaned == text
    assert removed == []


def test_publisher_status_stamp_is_removed():
    cleaned, removed = clean_extracted_text(
        "Published as a conference paper at ICLR 2024\nAbstract: we study robust learning."
    )
    assert cleaned == "Abstract: we study robust learning."
    assert removed[0][0] == "publication_watermark"


def test_footnote_detection_requires_explicit_marker_or_label():
    assert is_standalone_footnote("<sup>1</sup> Corresponding author: a@b.edu")
    assert is_standalone_footnote("1This work was supported by the National Science Foundation.")
    assert is_standalone_footnote("Corresponding author: a@b.edu")
    assert not is_standalone_footnote("1 Introduction")
    assert not is_standalone_footnote("Our method is described in arXiv:2401.01234.")


def test_clean_blocks_records_removed_noise_and_keeps_real_text():
    report = new_cleaning_report()
    blocks = [
        {"id": 1, "type": "text", "en": "arXiv:2401.01234v1 [cs.CV] 2 Feb 2024\nA useful body paragraph.",
         "zh": "", "meta": {"page": 1}},
        {"id": 2, "type": "text", "en": "<sup>2</sup> Corresponding author: a@b.edu",
         "zh": "", "meta": {"page": 1}},
        {"id": 3, "type": "formula", "en": r"\sum_i x_i", "zh": "", "meta": {"page": 2}},
    ]
    cleaned, report = clean_blocks(blocks, report)
    assert [b["id"] for b in cleaned] == [1, 3]
    assert cleaned[0]["en"] == "A useful body paragraph."
    assert cleaned[1]["type"] == "formula"
    assert report["removed_count"] == 2
    assert report["by_rule"]["arxiv_watermark"] == 1
    assert report["by_rule"]["explicit_footnote"] == 1


def test_repeated_running_header_requires_three_distinct_pages():
    header = "Proceedings of the Example Conference 2026"
    blocks = [
        {"id": i, "type": "text", "en": header, "zh": "", "meta": {"page": page}}
        for i, page in enumerate((1, 2, 3), start=1)
    ]
    blocks.append({"id": 4, "type": "text", "en": "Introduction",
                   "zh": "", "meta": {"page": 1}})
    cleaned, report = clean_blocks(blocks)
    assert [b["id"] for b in cleaned] == [4]
    assert report["by_rule"]["repeated_header_footer"] == 3


def test_duplicate_short_body_phrase_is_not_treated_as_running_header():
    phrase = "deep learning"
    blocks = [
        {"id": i, "type": "text", "en": phrase, "zh": "", "meta": {"page": i}}
        for i in (1, 2, 3)
    ]
    cleaned, _ = clean_blocks(blocks)
    assert len(cleaned) == 3
