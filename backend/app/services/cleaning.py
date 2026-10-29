"""High-confidence cleaning for publisher watermarks, PDF artifacts and footnotes.

The cleaner is intentionally conservative: remove standalone, recognizable metadata
stamps; use page geometry in the built-in PDF parser for ambiguous footnotes; and
only treat repeated short text as a running header/footer when it recurs across
multiple distinct pages. Normal body text mentioning arXiv is retained.
"""
from collections import defaultdict
import re

ARXIV_STAMP = re.compile(
    r"^\s*(?:"
    r"arxiv(?:\.org)?\s*[:/]\s*\d{4}\.\d{4,5}(?:v\d+)?"
    r"(?:\s*\[[^\]]{1,40}\])?"
    r"(?:\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})?"
    r"(?:\s+(?:submitted|updated|revised)\b.*)?"
    r"|arxiv(?:\.org)?\s+(?:preprint|e-prints?)\b.{0,120}"
    r"|(?:preprint|version)\s+available\s+(?:on|at)\s+arxiv\b.{0,120}"
    r"|\d{4}\.\d{4,5}(?:v\d+)?\s+\[[A-Za-z0-9.\-]+\]"
    r"(?:\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})?"
    r")\s*$",
    re.I,
)
PUBLICATION_STAMP = re.compile(
    r"^\s*(?:"
    r"published as (?:a|an) .{0,120}\b(?:paper|work)\b.{0,80}"
    r"|under review (?:at|for) .{0,100}"
    r"|accepted (?:at|for publication at|for publication in) .{0,100}"
    r"|to appear in .{0,120}"
    r"|preprint submitted to .{0,120}"
    r"|anonymous submission to .{0,120}"
    r")\s*$",
    re.I,
)
MARGIN_METADATA = re.compile(
    r"^\s*(?:"
    r"(?:©|\(c\))\s*\d{4}.*"
    r"|copyright\b.*|all rights reserved\.?|"
    r"creative commons\b.*|cc[- ]by(?:[- ][a-z0-9.]+)?\b.*|"
    r"acm reference format\b.*|ccs concepts\b.*|"
    r"doi\s*:\s*10\.\d{4,9}/\S+|https?://(?:dx\.)?doi\.org/\S+|"
    r"arxiv\s*:\s*\d{4}\.\d{4,5}.*|"
    r"proceedings of the .{0,120}|vol(?:ume)?\.?\s*\d+.{0,100}"
    r")\s*$",
    re.I,
)
SUP_FOOTNOTE = re.compile(r"^\s*<sup>\s*(?:\d+|[*†‡§])\s*</sup>", re.I)
FOOTNOTE_MARKER = re.compile(
    r"^\s*(?:<sup>\s*(?:\d+|[*†‡§])\s*</sup>|\[\d{1,2}\]|"
    r"\d{1,2}(?=[A-Za-z(])|[*†‡§])\s*(.*)$",
    re.I | re.S,
)
FOOTNOTE_HINT = re.compile(
    r"(?:corresponding author|equal contribution|contributed equally|"
    r"author to whom correspondence|for correspondence|e-?mail\s*:|"
    r"contact author|current address|now at\b|"
    r"this work (?:was )?(?:supported|funded)|supported in part by|"
    r"funded by|author contributions|https?://|\bwww\.)",
    re.I,
)
FOOTNOTE_LABEL = re.compile(
    r"^\s*(?:\*\s*)?(?:corresponding author|equal contribution|"
    r"authors? contributions|funding statement|for correspondence|e-?mail\s*:)",
    re.I,
)
PAGE_FOOTER = re.compile(
    r"^\s*(?:\d{1,4}|\d+[:：]\d+|[A-Z]\.\s*[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+et al\.?)\s*$"
)
REPEATED_TEXT_MIN = 15
REPEATED_TEXT_MAX = 90


def new_cleaning_report():
    return {"removed_count": 0, "by_rule": {}, "samples": []}


def record_removed(report, rule, text, page=None):
    if report is None:
        return
    report["removed_count"] = int(report.get("removed_count", 0)) + 1
    by_rule = report.setdefault("by_rule", {})
    by_rule[rule] = int(by_rule.get(rule, 0)) + 1
    samples = report.setdefault("samples", [])
    sample = " ".join(str(text or "").split())[:160]
    item = {"rule": rule, "text": sample}
    if page is not None:
        item["page"] = page
    if len(samples) < 12 and item not in samples:
        samples.append(item)


def is_standalone_footnote(text):
    """Recognize explicitly marked footnotes; ambiguous numbered prose is preserved."""
    raw = str(text or "").strip()
    if not raw:
        return False
    if SUP_FOOTNOTE.match(raw) or FOOTNOTE_LABEL.match(raw):
        return True
    match = FOOTNOTE_MARKER.match(raw)
    return bool(match and FOOTNOTE_HINT.search(match.group(1) or ""))


def _line_rule(line, margin=False):
    if ARXIV_STAMP.match(line):
        return "arxiv_watermark"
    if PUBLICATION_STAMP.match(line):
        return "publication_watermark"
    if margin and MARGIN_METADATA.match(line) and len(line) <= 180:
        return "publisher_margin_metadata"
    return ""


def clean_extracted_text(text, *, margin=False, page=None):
    """Strip recognized noise lines while retaining legitimate text in the same block.

    Returns (clean_text, [(rule, removed_line), ...]). The arXiv rule only matches
    a standalone identifier/stamp, not a sentence discussing arXiv in the paper.
    """
    raw = str(text or "").replace("\ufffd", "").replace("\xad", "")
    removed, kept = [], []
    for line in raw.splitlines() or [raw]:
        if not line.strip():
            kept.append(line)
            continue
        rule = _line_rule(line.strip(), margin=margin)
        if rule:
            removed.append((rule, line.strip()))
        else:
            kept.append(line)
    return "\n".join(kept).strip(), removed


def clean_blocks(blocks, report=None):
    """Apply conservative text rules plus cross-page repeated-header detection."""
    report = report if report is not None else new_cleaning_report()
    first_pass = []
    for original in blocks:
        block = dict(original)
        block_type = block.get("type")
        if block_type not in {"text", "heading", "list"}:
            first_pass.append(block)
            continue
        meta = block.get("meta") if isinstance(block.get("meta"), dict) else {}
        page = meta.get("page")
        en = block.get("en") or ""
        cleaned_en, removed = clean_extracted_text(en, page=page)
        for rule, sample in removed:
            record_removed(report, rule, sample, page)
        if cleaned_en.strip() and is_standalone_footnote(cleaned_en):
            record_removed(report, "explicit_footnote", cleaned_en, page)
            continue
        block["en"] = cleaned_en
        block["zh"] = str(block.get("zh") or "").replace("\ufffd", "").replace("\xad", "")
        if en.strip() and not cleaned_en.strip() and removed:
            continue
        first_pass.append(block)

    # Repeated headers/footers must recur on at least three distinct pages.
    # Short/common body fragments are deliberately excluded.
    page_sets = defaultdict(set)
    keys = {}
    for i, block in enumerate(first_pass):
        if block.get("type") != "text":
            continue
        value = " ".join(str(block.get("en") or "").split())
        meta = block.get("meta") if isinstance(block.get("meta"), dict) else {}
        page = meta.get("page")
        if not value or page is None or not (REPEATED_TEXT_MIN <= len(value) <= REPEATED_TEXT_MAX):
            continue
        key = re.sub(r"\d+[:：]?|\b\d{1,4}\b", "#", value.casefold())
        keys[i] = (key, value, page)
        page_sets[key].add(str(page))
    repeated = {key for key, pages in page_sets.items() if len(pages) >= 3}
    kept = []
    for i, block in enumerate(first_pass):
        info = keys.get(i)
        page = (block.get("meta") or {}).get("page")
        if info and info[0] in repeated:
            record_removed(report, "repeated_header_footer", info[1], page)
            continue
        text = str(block.get("en") or "").strip()
        if block.get("type") == "text" and PAGE_FOOTER.match(text):
            record_removed(report, "page_footer", text, page)
            continue
        kept.append(block)
    return kept, report
