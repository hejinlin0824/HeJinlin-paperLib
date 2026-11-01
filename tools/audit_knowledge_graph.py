"""Audit graph-inclusion scope without deleting or rewriting paper data.

Default behavior is dry-run. To classify one paper, pass an explicit paper id
and role; this only changes papers.library_role and leaves all evidence intact.
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import DB_PATH  # noqa: E402

VALID_ROLES = {"normal", "test", "excluded"}
SUSPICIOUS = re.compile(r"(?:\btest\b|\bdummy\b|\bmock\b|\bfixture\b|\bsample\b|\bdemo\b|测试|测试用|示例|样例)", re.I)


def main():
    parser = argparse.ArgumentParser(description="审计知识图谱论文纳入范围（默认只读）")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--set-role", nargs=2, metavar=("PAPER_ID", "ROLE"),
                        help="显式修改单篇论文分类；role: normal/test/excluded")
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"数据库不存在：{args.db}")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
        if "library_role" not in cols:
            raise SystemExit("数据库尚未迁移：请先启动新版 PaperLib 完成升级")
        if args.set_role:
            paper_id, role = args.set_role
            if role not in VALID_ROLES:
                raise SystemExit(f"无效 role：{role}")
            cur = conn.execute("UPDATE papers SET library_role=? WHERE id=?", (role, paper_id))
            if cur.rowcount != 1:
                raise SystemExit(f"论文 ID 不存在：{paper_id}")
            conn.commit()
            print(json.dumps({"updated": paper_id, "library_role": role}, ensure_ascii=False))
            return
        rows = conn.execute(
            "SELECT p.id,p.title,p.title_zh,p.status,COALESCE(p.library_role,'normal') role,"
            "(SELECT COUNT(*) FROM kg_evidence e WHERE e.paper_id=p.id) evidence_count,"
            "(SELECT COUNT(DISTINCT relation_id) FROM kg_evidence e WHERE e.paper_id=p.id "
            "AND COALESCE(relation_id,'')!='') relation_count,"
            "(SELECT COUNT(DISTINCT entity_id) FROM kg_evidence e WHERE e.paper_id=p.id "
            "AND COALESCE(entity_id,'')!='') entity_count "
            "FROM papers p ORDER BY p.created_at"
        ).fetchall()
        report = []
        for p in rows:
            title = p["title_zh"] or p["title"] or ""
            report.append({
                "id": p["id"], "title": title, "status": p["status"],
                "library_role": p["role"], "evidence_count": p["evidence_count"],
                "relation_count": p["relation_count"], "entity_count": p["entity_count"],
                "suggested_review": bool(SUSPICIOUS.search(title)) and p["role"] == "normal",
            })
        print(json.dumps({
            "database": str(args.db), "mode": "dry-run",
            "paper_count": len(report),
            "suspicious_candidates": sum(x["suggested_review"] for x in report),
            "papers": report,
            "note": "疑似测试论文只做提示；不会自动改分类、删除论文或删除图谱证据。",
        }, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
