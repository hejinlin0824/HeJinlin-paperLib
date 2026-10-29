# -*- coding: utf-8 -*-
"""清理已经入库的论文噪声块。

默认只预览；只有明确传入 --apply 才会写数据库。应用前自动生成 SQLite 一致性备份。
支持按 paper_id 处理单篇，也可以不传 ID 处理全部论文。
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services.cleaning import (clean_blocks, new_cleaning_report, record_removed)
from app.services.sections import rebuild_sections

DB = ROOT / "data" / "library.db"
HTML_SUP_RE = re.compile(r"<sup>(.*?)</sup>", re.I | re.S)
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _parse_meta(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _clean_markup(block):
    """Clean presentation-only legacy HTML on text blocks, not table/formula payloads."""
    if block.get("type") not in {"text", "heading", "list"}:
        return block
    # Inline superscripts are citations; standalone marked footnotes have already
    # been detected by clean_blocks before tags are flattened.
    for field in ("en", "zh"):
        value = str(block.get(field) or "")
        value = HTML_SUP_RE.sub(r"[\1]", value)
        value = HTML_TAG_RE.sub("", value)
        value = value.replace("\ufffd", "").replace("\xad", "")
        block[field] = value
    return block


def _backup_database():
    if not DB.exists():
        raise FileNotFoundError(f"数据库不存在：{DB}")
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"library.db.preclean-{stamp}"
    with sqlite3.connect(DB, timeout=20) as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
    return backup_path


def _paper_ids(connection, requested):
    if requested:
        return requested
    return [r[0] for r in connection.execute("SELECT id FROM papers ORDER BY created_at")]


def clean_one(connection, pid, apply=False):
    paper = connection.execute(
        "SELECT title, cleaning_report FROM papers WHERE id=?", (pid,)
    ).fetchone()
    if not paper:
        print(f"[{pid}] 不存在")
        return False

    connection.row_factory = sqlite3.Row
    original = [dict(r) for r in connection.execute(
        "SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,)
    )]
    for block in original:
        block["meta"] = _parse_meta(block.get("meta"))
    old_report = _parse_meta(paper["cleaning_report"])
    report = old_report if isinstance(old_report.get("by_rule"), dict) else new_cleaning_report()

    # First pass identifies explicitly marked footnotes before legacy HTML is flattened.
    kept, report = clean_blocks(original, report)
    kept = [_clean_markup(b) for b in kept]
    kept, report = clean_blocks(kept, report)

    kept_ids = {b["id"] for b in kept}
    annotated_ids = {r[0] for r in connection.execute(
        "SELECT block_id FROM annotations WHERE paper_id=? AND block_id IS NOT NULL", (pid,)
    )}
    # Keep an invisible tombstone when a removed block has annotations, so their block
    # IDs remain stable and the notes do not become detached from the reading timeline.
    kept_by_id = {b["id"]: b for b in kept}
    for block in original:
        if block["id"] in kept_ids or block["id"] not in annotated_ids:
            continue
        meta = dict(block.get("meta") or {})
        meta["cleaned_placeholder"] = True
        kept_by_id[block["id"]] = {
            **block, "type": "cleaned", "en": "", "zh": "",
            "meta": meta, "translate_status": "done",
        }
    final_blocks = sorted(kept_by_id.values(), key=lambda b: b.get("order_idx", 0))
    removed = len(original) - len(final_blocks)
    changed = any(
        (o.get("en") != n.get("en") or o.get("zh") != n.get("zh")
         or o.get("type") != n.get("type"))
        for o in original for n in final_blocks if o["id"] == n["id"]
    )

    print(f"[{pid}] {(paper['title'] or '')[:48]} | 原块 {len(original)} -> {len(final_blocks)} | "
          f"删除 {removed} 块 | 新报告 {report.get('by_rule', {})}")
    if not apply:
        return False
    if not removed and not changed and report == old_report:
        return False

    connection.execute("DELETE FROM blocks WHERE paper_id=?", (pid,))
    for index, block in enumerate(final_blocks):
        connection.execute(
            "INSERT INTO blocks(id, paper_id, order_idx, type, en, zh, meta, translate_status, plain, ask) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (block["id"], pid, index, block["type"], block.get("en", ""), block.get("zh", ""),
             json.dumps(block.get("meta") or {}, ensure_ascii=False),
             block.get("translate_status", "done"), block.get("plain", ""), block.get("ask", "")),
        )
    connection.execute(
        "UPDATE papers SET cleaning_report=?, updated_at=datetime('now') WHERE id=?",
        (json.dumps(report, ensure_ascii=False), pid),
    )
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_id", nargs="*", help="可选：只清理指定论文 ID")
    parser.add_argument("--apply", action="store_true", help="实际写入数据库；默认仅预览")
    args = parser.parse_args()

    if not DB.exists():
        raise SystemExit(f"数据库不存在：{DB}；请先运行一次 PaperLib。")

    with sqlite3.connect(DB, timeout=20) as connection:
        connection.row_factory = sqlite3.Row
        cols = {r[1] for r in connection.execute("PRAGMA table_info(papers)")}
        missing_report_column = "cleaning_report" not in cols
        if missing_report_column and not args.apply:
            raise SystemExit("数据库尚未迁移到新版本。请先启动新版 PaperLib，或在 --apply 模式下由脚本备份后迁移。")
        ids = _paper_ids(connection, args.paper_id)

    if args.apply:
        backup = _backup_database()
        print(f"备份已创建：{backup}")
        if missing_report_column:
            with sqlite3.connect(DB, timeout=20) as migration:
                migration.execute("ALTER TABLE papers ADD COLUMN cleaning_report TEXT DEFAULT '{}'")
                migration.commit()
        changed_ids = []
        with sqlite3.connect(DB, timeout=20) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            for pid in ids:
                if clean_one(connection, pid, apply=True):
                    changed_ids.append(pid)
            connection.commit()
        for pid in changed_ids:
            try:
                rebuild_sections(pid)
            except Exception as exc:
                print(f"[{pid}] 章节索引重建失败（正文清洗已提交，请检查日志）：{exc}")
        print(f"完成：更新 {len(changed_ids)} 篇论文。")
    else:
        with sqlite3.connect(DB, timeout=20) as connection:
            connection.row_factory = sqlite3.Row
            for pid in ids:
                clean_one(connection, pid, apply=False)
        print("当前为预览模式，没有修改数据库。确认报告后运行：python tools/clean_paper.py --apply [paper_id ...]")


if __name__ == "__main__":
    main()
