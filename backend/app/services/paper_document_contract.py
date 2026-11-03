"""Stable, testable contract for MinerU-derived structured paper documents.

This module does not parse PDFs. It validates and normalizes the blocks already
produced by MinerU's content_list.json adapter, retaining source provenance.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

DOCUMENT_BLOCK_TYPES = {"heading", "text", "list", "table", "formula", "image", "references"}


def normalize_mineru_document(blocks: list[dict[str, Any]], engine: str = "mineru") -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    for position, raw in enumerate(blocks):
        if not isinstance(raw, dict):
            warnings.append(f"block[{position}] is not an object")
            continue
        kind = str(raw.get("type") or "text").lower()
        if kind not in DOCUMENT_BLOCK_TYPES:
            warnings.append(f"block[{position}] has unsupported type {kind!r}; preserved as text")
            kind = "text"
        meta = dict(raw.get("meta") or {})
        source = dict(meta.get("source") or {})
        source.setdefault("engine", engine)
        source.setdefault("order", raw.get("order_idx", position))
        source.setdefault("page", meta.get("page"))
        source.setdefault("mineru_type", meta.get("mineru_type"))
        if meta.get("bbox") is not None:
            source.setdefault("bbox", meta["bbox"])
        meta["source"] = source
        item = {
            "type": kind,
            "en": str(raw.get("en") or ""),
            "meta": meta,
            "order_idx": int(raw.get("order_idx", position)),
        }
        if kind == "table":
            item["meta"].setdefault("table_html", item["meta"].get("html"))
            item["meta"].setdefault("table_markdown", item["meta"].get("md"))
        if kind == "formula":
            item["meta"].setdefault("latex", item["meta"].get("latex") or item["en"])
        normalized.append(item)
    normalized.sort(key=lambda item: item["order_idx"])
    counts = Counter(item["type"] for item in normalized)
    return {
        "schema_version": "paper-document/1.0",
        "source_engine": engine,
        "blocks": normalized,
        "stats": {"block_count": len(normalized), "type_counts": dict(counts)},
        "warnings": warnings,
    }


def validate_mineru_document(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if document.get("schema_version") != "paper-document/1.0":
        errors.append("unsupported schema_version")
    blocks = document.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        errors.append("blocks must be a non-empty list")
        return errors
    seen_orders: set[int] = set()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            errors.append(f"blocks[{index}] must be an object")
            continue
        if block.get("type") not in DOCUMENT_BLOCK_TYPES:
            errors.append(f"blocks[{index}] has invalid type")
        if not isinstance(block.get("en"), str):
            errors.append(f"blocks[{index}].en must be a string")
        if not isinstance(block.get("meta"), dict):
            errors.append(f"blocks[{index}].meta must be an object")
        order = block.get("order_idx")
        if not isinstance(order, int) or order in seen_orders:
            errors.append(f"blocks[{index}].order_idx must be a unique integer")
        else:
            seen_orders.add(order)
        source = (block.get("meta") or {}).get("source")
        if not isinstance(source, dict) or not source.get("engine"):
            errors.append(f"blocks[{index}] is missing source provenance")
        if block.get("type") == "formula" and not (block.get("meta") or {}).get("latex"):
            errors.append(f"blocks[{index}] is missing formula LaTeX")
        if block.get("type") == "table":
            meta = block.get("meta") or {}
            if not any(meta.get(key) for key in ("table_html", "table_markdown", "html", "md")):
                errors.append(f"blocks[{index}] is missing table body")
    return errors
