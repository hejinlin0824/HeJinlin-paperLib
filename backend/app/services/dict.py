"""选区查词：本地 ECDICT 词典 + 已有 LLM 缓存（不再调用 LLM，零 token 消耗）。"""
import re

from ..db import row


def _variants(w: str):
    """简单词形归一：复数/时态/比较级等还原到原形尝试。"""
    vs = [w]
    if w.endswith("ies") and len(w) > 4:
        vs.append(w[:-3] + "y")
    if w.endswith("es") and len(w) > 4:
        vs.append(w[:-2])
    if w.endswith("s") and len(w) > 3:
        vs.append(w[:-1])
    if w.endswith("ed") and len(w) > 4:
        vs += [w[:-2], w[:-1]]
    if w.endswith("ing") and len(w) > 5:
        vs += [w[:-3], w[:-3] + "e"]
    if w.endswith("er") and len(w) > 4:
        vs.append(w[:-2])
    if w.endswith("ly") and len(w) > 4:
        vs.append(w[:-2])
    return vs


def _local_lookup(w: str):
    for v in _variants(w):
        r = row("SELECT word, phonetic, translation FROM stardict WHERE word=?", (v,))
        if r and (r["translation"] or "").strip():
            return r
    return None


async def word_card(word: str, ctx: str = "") -> dict:
    w = (word or "").strip().lower()
    if not w or not re.fullmatch(r"[a-z][a-z\-' ]{0,40}", w):
        raise RuntimeError("不是一个有效的英文词")
    # ① 历史生成的词卡缓存（零成本直接用）
    cached = row("SELECT * FROM dict_cards WHERE word=?", (w,))
    if cached:
        return cached
    # ② 本地离线词典（ECDICT 130 万词，毫秒级零成本）
    local = _local_lookup(w)
    if local:
        return {"word": local["word"], "phonetic": local["phonetic"],
                "meaning": local["translation"], "context_note": ""}
    # 词表没有：不调用 LLM，前端提示
    return {"word": w, "phonetic": "", "meaning": "", "context_note": "", "not_found": True}
