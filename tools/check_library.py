"""Read-only guard for the local PaperLib library before starting the server.

This script never creates, migrates, repairs, or deletes the database. It reports the
exact database path and refuses startup when paper files exist but the database is
missing/empty, preventing an accidental empty-library launch from hiding user data.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "library.db"
PAPERS_DIR = DATA_DIR / "papers"


def inspect_library(root: Path = ROOT) -> dict:
    data_dir = root / "data"
    db_path = data_dir / "library.db"
    papers_dir = data_dir / "papers"
    pdfs = list(papers_dir.rglob("*.pdf")) + list(papers_dir.rglob("*.PDF")) if papers_dir.exists() else []
    paper_dirs = [p for p in papers_dir.iterdir() if p.is_dir()] if papers_dir.exists() else []

    result = {
        "database": str(db_path.resolve()),
        "database_exists": db_path.is_file(),
        "database_bytes": db_path.stat().st_size if db_path.is_file() else 0,
        "paper_records": None,
        "normal_paper_records": None,
        "paper_roles": {},
        "extracted_paper_records": None,
        "pdf_files": len(pdfs),
        "paper_folders": len(paper_dirs),
        "safe_to_start": True,
        "reason": "",
    }

    if not db_path.is_file():
        if pdfs or paper_dirs:
            result.update(safe_to_start=False,
                          reason="论文文件夹存在，但 data/library.db 不存在；为避免初始化空库而隐藏数据，已阻止启动。")
        else:
            result["reason"] = "首次运行：尚无数据库和论文文件，可以正常初始化。"
        return result

    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "papers" not in tables:
                result.update(safe_to_start=False, reason="数据库存在但缺少 papers 表；数据库可能不是 PaperLib 数据库。")
                return result
            result["paper_records"] = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
            if "library_role" in columns:
                role_rows = conn.execute(
                    "SELECT COALESCE(library_role, 'normal'), COUNT(*) FROM papers GROUP BY 1"
                ).fetchall()
                result["paper_roles"] = {role: count for role, count in role_rows}
                result["normal_paper_records"] = result["paper_roles"].get("normal", 0)
            else:
                result["normal_paper_records"] = result["paper_records"]
            if "kg_evidence" in tables:
                result["extracted_paper_records"] = conn.execute(
                    "SELECT COUNT(DISTINCT paper_id) FROM kg_evidence"
                ).fetchone()[0]
    except sqlite3.Error as exc:
        result.update(safe_to_start=False, reason=f"数据库无法只读打开：{exc}")
        return result

    if result["paper_records"] == 0 and (pdfs or paper_dirs):
        result.update(safe_to_start=False,
                      reason="数据库论文记录为 0，但论文文件夹仍有内容；已阻止启动，请先恢复/核对数据库。")
    elif result["paper_records"] == 0:
        result["reason"] = "数据库当前没有论文记录；未发现可匹配的论文文件。"
    else:
        result["reason"] = "数据库检查通过。"
    return result


def main() -> int:
    # Windows consoles may use cp1252/cp936; never let diagnostic text crash startup.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    report = inspect_library()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["safe_to_start"]:
        print("\n[SAFE GUARD] 未修改任何文件，服务不会启动。请先备份整个 data 目录并核对数据库。")
        return 2
    print(f"\n[LIBRARY] 数据库论文：{report['paper_records'] if report['paper_records'] is not None else '尚未初始化'} 篇")
    if report["normal_paper_records"] is not None:
        print(f"[LIBRARY] 正常纳入知识图谱：{report['normal_paper_records']} 篇；分类分布：{report['paper_roles']}")
    if report["extracted_paper_records"] is not None:
        print(f"[LIBRARY] 已有知识抽取证据：{report['extracted_paper_records']} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
