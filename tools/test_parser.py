# -*- coding: utf-8 -*-
"""用真实论文 PDF 单测内置解析器：检查标题提取与块结构。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.services import parser_builtin  # noqa: E402

pdf = sys.argv[1]
out_dir = Path(__file__).resolve().parent.parent / "data" / "_parse_test"
out_dir.mkdir(parents=True, exist_ok=True)
result = parser_builtin.parse(pdf, out_dir)
print("== meta ==")
print("title:", result["meta"]["title"])
print("year :", result["meta"]["year"])
print("pages:", result["meta"]["pages"])
print("== first 8 blocks ==")
for b in result["blocks"][:8]:
    en = (b["en"] or "").replace("\n", " ")[:70]
    print(f"{b['order_idx']:3d} {b['type']:10s} lv{b.get('meta', {}).get('level', '-')} p{b.get('meta', {}).get('page', '-')} | {en}")
print("total blocks:", len(result["blocks"]))
