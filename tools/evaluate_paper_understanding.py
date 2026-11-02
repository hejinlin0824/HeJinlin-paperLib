"""Evaluate a saved understanding draft against a manually curated JSON benchmark.

Read-only: does not modify the database or paper content.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import row, rows
from app.services.paper_quality_eval import evaluate_payload
from app.util import loads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft-id", required=True)
    parser.add_argument("--benchmark", default=str(ROOT / "tests/fixtures/harness-theory-v1.json"))
    args = parser.parse_args()
    draft = row("SELECT id,paper_id,version,payload_json FROM kg_understanding_drafts WHERE id=?",
                (args.draft_id,))
    if not draft:
        raise SystemExit("Draft not found.")
    gold = json.loads(Path(args.benchmark).read_text(encoding="utf-8"))
    blocks = rows("SELECT id,en FROM blocks WHERE paper_id=?", (draft["paper_id"],))
    block_map = {int(item["id"]): str(item.get("en") or "") for item in blocks}
    payload = loads(draft.get("payload_json"), {})
    report = evaluate_payload(payload, gold, block_map)
    report["draft_id"] = draft["id"]
    report["paper_id"] = draft["paper_id"]
    report["draft_version"] = draft["version"]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
