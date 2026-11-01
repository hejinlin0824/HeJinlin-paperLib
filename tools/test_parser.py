# -*- coding: utf-8 -*-
"""用真实论文 PDF 手动检查内置解析器。

此脚本需要显式提供 PDF 路径，不属于 pytest 自动化测试：
    python tools/test_parser.py <paper.pdf>
"""
import argparse
from pathlib import Path


def main(pdf_path: str) -> int:
    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    import sys

    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from app.services import parser_builtin

    pdf = Path(pdf_path).expanduser().resolve()
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF 文件不存在：{pdf}")

    out_dir = Path(__file__).resolve().parent.parent / "data" / "_parse_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = parser_builtin.parse(str(pdf), out_dir)

    print("== meta ==")
    print("title:", result["meta"]["title"])
    print("year :", result["meta"]["year"])
    print("pages:", result["meta"]["pages"])
    print("== first 8 blocks ==")
    for block in result["blocks"][:8]:
        english = (block["en"] or "").replace("\n", " ")[:70]
        metadata = block.get("meta", {})
        print(
            f"{block['order_idx']:3d} {block['type']:10s} "
            f"lv{metadata.get('level', '-')} p{metadata.get('page', '-')} | {english}"
        )
    print("total blocks:", len(result["blocks"]))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="手动检查 PaperLib 内置 PDF 解析器")
    parser.add_argument("pdf", help="待解析的 PDF 文件路径")
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.pdf))
