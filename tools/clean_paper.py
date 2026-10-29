# -*- coding: utf-8 -*-
"""论文数据清理脚本：对已入库的块做就地卫生清理（无需重新解析/翻译）。

清理项（历次反馈固化）：
1. 脚注块：以 <sup>数字</sup> 开头的块（MinerU 把页底脚注当正文返回）→ 删除
2. HTML 标签残留：内联 <sup>n</sup> → [n]，其余标签剥壳保内容
3. 模板噪声：版权/许可/DOI/CCS/引用格式块/基金句/作者联系方式 → 删除
4. 孤立编号块："1"、"263:3" 等纯数字/标点块 → 删除
5. 页脚重复块：数字归一化后重复 ≥3 次的短块 → 删除
6. 乱码：U+FFFD 替换字符、软连字符 → 剥除

用法：python tools/clean_paper.py [paper_id ...]   # 缺省清理全部论文
"""
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
DB = Path(__file__).resolve().parent.parent / "data" / "library.db"

SUP_START = re.compile(r"^\s*<sup>\s*\d+\s*</sup>", re.I)
HTML_SUP = re.compile(r"<sup>(.*?)</sup>", re.I | re.S)
HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
NOISE = re.compile(
    r"^(CCS Concepts|ACM Reference Format|©|\(c\)\s*\d{4}|Copyright|This work is licensed|"
    r"Creative Commons|CC[ -]BY|Attribution\s*(4\.0|NonCommercial|ShareAlike)|"
    r"https?://(dx\.)?doi\.org|doi\.org/|ACM\s+\d{4}-\d{4}|ACM Comput\. Surv|ACM Trans\.|"
    r"Proc\.\s*of|Proceedings of the|Vol(ume)?\.?\s*\d+|Article\s*(No\.?\s*)?\d{1,4}\.?\s*$|"
    r"Publication date|出版日期|版权归|版权所有|Authors?['’]?\s*(Contact|address)|通讯作者|"
    r"^[A-Z][a-z]+ \d{1,2}, \d{4}$|"
    r".{0,160}(research|work|studies?) (is|are)? ?supported( in part| in whole| partly| partially)? by)",
    re.I)
CITATION_FMT = re.compile(
    r".{0,250}(ACM (Comput\. Surv|Trans\.)\s*\d+,\s*\d+|"
    r"Article\s*\d+\s*\(\w+\s+\d{4}\),\s*\d+\s*pages?)")
ORPHAN_DIGIT = re.compile(r"[\d\s:：;,.]+")


def clean_blocks(blocks):
    """返回 (保留块列表, 删除计数, 修改计数)。"""
    drops = Counter()
    modified = 0

    def key(t):
        return re.sub(r"\d+", "#", (t or "").strip())

    # 页脚重复度统计
    cnt = Counter(key(b["en"]) for b in blocks
                  if b.get("type") == "text" and 0 < len((b["en"] or "").strip()) <= 90)
    dup = {k for k, c in cnt.items() if c >= 3}

    kept = []
    for b in blocks:
        if b.get("type") in ("text", "heading", "list"):
            en = b.get("en") or ""
            zh = b.get("zh") or ""
            if SUP_START.match(en.strip()) or SUP_START.match(zh.strip()):
                drops["脚注块"] += 1
                continue
            new_en = HTML_SUP.sub(r"[\1]", en)
            new_en = HTML_TAG.sub("", new_en)
            new_en = new_en.replace("\ufffd", "").replace("\xad", "")
            new_zh = HTML_SUP.sub(r"[\1]", zh)
            new_zh = HTML_TAG.sub("", new_zh)
            new_zh = new_zh.replace("\ufffd", "").replace("\xad", "")
            if new_en.strip() and (ORPHAN_DIGIT.fullmatch(new_en.strip())
                                   or NOISE.match(" ".join(new_en.split()))
                                   or CITATION_FMT.match(" ".join(new_en.strip()))):
                drops["噪声块"] += 1
                continue
            if b.get("type") == "text" and new_en.strip() and (
                    PAGE_FOOTER_LIKE(new_en) or (key(new_en) in dup and len(new_en.strip()) <= 90)):
                drops["页脚块"] += 1
                continue
            if new_en != en or new_zh != zh:
                modified += 1
            b["en"], b["zh"] = new_en, new_zh
        kept.append(b)
    return kept, drops, modified


def PAGE_FOOTER_LIKE(t):
    t = t.strip()
    return bool(re.fullmatch(r"\d+[:：]\d+", t) or re.fullmatch(r"\d{1,4}", t)
                or re.match(r"^[A-Z]\.\s*[A-Z][a-z]+.*et al\.?$", t))


def clean_paper(pid):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    p = c.execute("SELECT title FROM papers WHERE id=?", (pid,)).fetchone()
    if not p:
        print(f"[{pid}] 不存在")
        return
    blocks = [dict(r) for r in c.execute(
        "SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))]
    kept, drops, modified = clean_blocks(blocks)
    # 重排 order_idx 并写回
    c.execute("DELETE FROM blocks WHERE paper_id=?", (pid,))
    for i, b in enumerate(kept):
        c.execute(
            "INSERT INTO blocks(id, paper_id, order_idx, type, en, zh, meta, translate_status, plain, ask) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (b["id"], pid, i, b["type"], b["en"], b["zh"], b["meta"],
             b.get("translate_status", "done"), b.get("plain", ""), b.get("ask", "")))
    c.commit()
    drops_total = sum(drops.values())
    print(f"[{pid}] {(p['title'] or '')[:36]} | 删除 {drops_total} 块 {dict(drops)} | 修正 {modified} 块 | 保留 {len(kept)}")


if __name__ == "__main__":
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    ids = sys.argv[1:] or [r["id"] for r in c.execute("SELECT id FROM papers")]
    for pid in ids:
        clean_paper(pid)
