# -*- coding: utf-8 -*-
"""论文质量自动检查：把历次反馈的问题固化为检查项，防止回归。

用法：python tools/quality_check.py [paper_id ...]   # 缺省检查全部论文
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

DB = Path(__file__).resolve().parent.parent / "data" / "library.db"

NOISE = re.compile(
    r"^(CCS Concepts|ACM Reference Format|©|\(c\)\s*\d{4}|This work is licensed|"
    r"Creative Commons|CC[ -]BY|https?://(dx\.)?doi\.org|doi\.org/|"
    r"ACM\s+\d{4}-\d{4}|Publication date|出版日期|Authors?['’]?\s*(Contact|address)|"
    r"\d+[:：]\d+\s*$|[A-Z]\.\s*[A-Z][a-z]+\s+et al\.?\s*$|\d{1,4}\s*$|"
    r"^[A-Z][a-z]+ \d{1,2}, \d{4}$)", re.I)
CITATION_FMT = re.compile(
    r".{0,250}(ACM (Comput\. Surv|Trans\.)\s*\d+,\s*\d+|Article\s*\d+\s*\(\w+\s+\d{4}\),\s*\d+\s*pages?)")
INLINE_HEAD = re.compile(
    r"^(\d{1,2}(?:\.\d{1,2}){1,3})\s+([A-Z\u4e00-\u9fff][^.。]{2,60}?)[.。]\s*(.{30,})$", re.S)
LIST_ITEM = re.compile(r"^[—–•\-\*]\s+\S|^\d{1,2}[.)]\s+\S")
BAD_TITLE_END = re.compile(r"\b(a|an|the|of|for|and|in|on|with|：|:|—|-)\s*$", re.I)


def check(pid):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    p = c.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    if not p:
        print(f"[{pid}] 不存在")
        c.close()
        return False
    bs = [dict(r) for r in c.execute(
        "SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))]
    secs = [dict(r) for r in c.execute(
        "SELECT * FROM sections WHERE paper_id=? ORDER BY order_idx", (pid,))]
    results = []

    def item(name, ok, detail=""):
        results.append((name, ok, detail))

    # 1 标题完整性与中文标题
    t = (p["title"] or "").strip()
    item("标题完整", len(t) > 12 and not BAD_TITLE_END.match(t), t[:60])
    item("中文标题", bool((p["title_zh"] or "").strip()), (p["title_zh"] or "")[:40])

    # 2 模板噪声残留（含孤立编号块）
    noise = [b for b in bs if b["type"] == "text" and (
        NOISE.match(" ".join((b["en"] or "").split()))
        or CITATION_FMT.match(" ".join((b["en"] or "").split()))
        or re.fullmatch(r"[\d\s:：;,.]+", (b["en"] or "").strip()))]
    item("无模板噪声(版权/DOI/引用格式/页脚)", len(noise) == 0,
         "; ".join((b["en"] or "")[:40] for b in noise[:2]))

    # 3 页眉重复（归一化后 ≥3 的长块）
    cnt = Counter(re.sub(r"\d+", "#", (b["en"] or "").strip())
                  for b in bs if b["type"] == "text" and 0 < len((b["en"] or "").strip()) <= 90)
    heads = [k for k, n in cnt.items() if n >= 3]
    item("无重复页眉", len(heads) == 0, heads[:1])

    # 4 内联标题粘连
    inline = [b for b in bs if b["type"] == "text" and INLINE_HEAD.match(b["en"] or "")]
    item("小节标题已拆分", len(inline) == 0,
         "; ".join((b["en"] or "")[:40] for b in inline[:2]))

    # 5 列表碎片（连续 ≥3 个条目块）
    run = mx = 0
    for b in bs:
        if b["type"] == "text" and LIST_ITEM.match(b["en"] or "") and len(b["en"] or "") < 600:
            run += 1; mx = max(mx, run)
        else:
            run = 0
    item("列表已合并", mx < 3, f"最长连续条目块 {mx}")

    # 6 公式 $$ 包裹残留
    badf = [b for b in bs if b["type"] == "formula" and
            (b["en"] or "").strip().startswith("$$")]
    item("公式无$$包裹", len(badf) == 0)

    # 7 乱码字符 / 脚注残留 / HTML 标签
    fffd = sum((b["en"] or "").count("\ufffd") + (b["zh"] or "").count("\ufffd") for b in bs)
    item("无乱码字符(U+FFFD)", fffd == 0, f"{fffd} 处")
    foot = [b for b in bs if re.match(r"^\s*<sup>\s*\d+", b["en"] or "")
            or re.match(r"^\s*<sup>\s*\d+", b["zh"] or "")]
    html = [b for b in bs if re.search(r"</?(sup|sub|b|i|u|em|strong|span)[^>]*>", b["en"] or "")]
    item("无脚注/HTML残留", not foot and not html,
         f"脚注 {len(foot)} / HTML {len(html)}")

    # 8 翻译完成度
    todo = [b for b in bs if b["type"] not in ("references", "authors", "image", "formula")
            and (b["en"] or "").strip()
            and (not (b["zh"] or "").strip() or b["translate_status"] != "done")]
    item("翻译完成", len(todo) == 0, f"{len(todo)} 段未完成")

    # 9 参考文献聚合
    import json as _j
    refs = [b for b in bs if b["type"] == "references"]
    n_entries = 0
    if refs:
        try:
            meta = _j.loads(refs[0]["meta"]) if isinstance(refs[0]["meta"], str) else refs[0]["meta"]
            n_entries = len(meta.get("entries") or [])
        except Exception:
            pass
    item("参考文献聚合", bool(refs) and n_entries >= 1, f"{n_entries} 条")

    # 10 章节切分
    item("章节切分", len(secs) >= 3, f"{len(secs)} 章")

    # 11 作者卡
    item("作者卡", any(b["type"] == "authors" for b in bs))

    # 12 空标题块/空正文块
    empties = [b for b in bs if b["type"] in ("text", "heading") and not (b["en"] or "").strip()]
    item("无空块", len(empties) == 0, f"{len(empties)} 个")

    # 新增回归：独立 arXiv / 期刊状态水印和明确脚注不能混进待翻译正文。
    stamps = [b for b in bs if b["type"] in ("text", "heading", "list")
              and any(ARXIV_STAMP.match(line.strip()) or PUBLICATION_STAMP.match(line.strip())
                      for line in (b["en"] or "").splitlines() if line.strip())]
    item("无 arXiv/出版状态水印", len(stamps) == 0,
         "; ".join((b["en"] or "")[:60] for b in stamps[:2]))
    footnote_blocks = [b for b in bs if b["type"] in ("text", "heading", "list")
                       and is_standalone_footnote(b["en"] or "")]
    item("无明确脚注块", len(footnote_blocks) == 0,
         "; ".join((b["en"] or "")[:60] for b in footnote_blocks[:2]))

    fails = [r for r in results if not r[1]]
    status = "✅ 全部通过" if not fails else f"❌ {len(fails)} 项未过"
    print(f"\n===== [{pid}] {t[:40]} =====")
    print(f"块 {len(bs)} | 章 {len(secs)} | {status}")
    for name, ok, detail in results:
        mark = "✅" if ok else "❌"
        line = f"  {mark} {name}" + (f"  ({detail})" if detail and not ok else "")
        print(line)
    c.close()
    return not fails


if __name__ == "__main__":
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    ids = sys.argv[1:] or [r["id"] for r in c.execute("SELECT id FROM papers")]
    ok = True
    for pid in ids:
        ok = check(pid) and ok
    sys.exit(0 if ok else 1)
