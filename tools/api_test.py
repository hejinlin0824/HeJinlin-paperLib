# -*- coding: utf-8 -*-
"""API 冒烟测试（中文正文，避免 shell 编码问题）。"""
import sys
import httpx

BASE = "http://127.0.0.1:8300"
c = httpx.Client(base_url=BASE, timeout=30)

pid = None
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("PID="):
        pid = line.strip().split("=", 1)[1]
print("pid:", pid)

# 1. 研究总表写入 + 读取
r = c.put(f"/api/log/{pid}", json={
    "research_problem": "如何高效阅读翻译论文",
    "solution": "块级双语对齐 + 本地工作台",
    "innovation": "批注锚定块ID，重译不丢",
    "my_thoughts": "很适合科研日常",
    "takeaway": "读论文效率翻倍",
    "status": "读完",
})
print("PUT log:", r.status_code, r.json().get("status"))

rows = c.get("/api/log").json()
r0 = rows[0]
print(f"log rows={len(rows)} status={r0['log_status']} open_q={r0['open_questions']} "
      f"total_q={r0['total_questions']} problem={r0['research_problem'][:12]}")

# 2. 导出 CSV / MD / 论文MD
csv_text = c.get("/api/log/export?format=csv").text
print("csv head:", csv_text[:60].replace("\n", "|"))
md_text = c.get("/api/log/export?format=md").text
print("md has takeaway:", "读论文效率翻倍" in md_text)
pmd = c.get(f"/api/papers/{pid}/export.md").text
print("paper md len:", len(pmd), "| has 图注:", "Overview of the parsing" in pmd)

# 3. 问题追踪：先从阅读器创建问题批注（联动建问题），再回答 + 解决
blocks = c.get(f"/api/papers/{pid}/blocks").json()
bid = [x for x in blocks if x["type"] == "text"][0]["id"]
c.post(f"/api/papers/{pid}/annotations", json={
    "kind": "question", "block_id": bid, "surface": "zh",
    "anchor_text": "", "content": "为什么用块级对齐？"})
qs = c.get(f"/api/questions?paper_id={pid}").json()
q = qs[0]
print("question:", q["content"], "| status:", q["status"])
c.patch(f"/api/questions/{q['id']}", json={"answer": "因为段落ID稳定", "status": "resolved"})
qs = c.get(f"/api/questions?paper_id={pid}").json()
print("after resolve:", qs[0]["status"], "| answer:", qs[0]["answer"])

# 4. 术语表
c.post("/api/glossary", json={"term_en": "attention", "term_zh": "注意力机制", "note": ""})
g = c.get("/api/glossary").json()
print("glossary:", [(x["term_en"], x["term_zh"], x["source"]) for x in g][:3])

# 5. 单段重译 + 编辑译文
blocks = c.get(f"/api/papers/{pid}/blocks").json()
b = [x for x in blocks if x["type"] == "text"][0]
nb = c.post(f"/api/papers/{pid}/blocks/{b['id']}/retranslate").json()
print("retranslate:", nb["zh"][:30])
nb2 = c.patch(f"/api/papers/{pid}/blocks/{b['id']}", json={"zh": "人工修正后的译文"}).json()
print("edit zh:", nb2["zh"])

# 6. 阅读进度 + 仪表盘 + 备份
c.put(f"/api/papers/{pid}/progress", json={"block_idx": 5, "ratio": 0.42})
d = c.get("/api/papers/dashboard").json()
print("dashboard:", d["stats"], "| recent:", len(d["recent"]), "| open_q:", len(d["open_questions"]))
r = c.get("/api/backup.zip")
print("backup:", r.status_code, len(r.content), "bytes")

# 7. 引擎测试
t = c.post("/api/settings/test-translate").json()
print("test-translate(mock):", t["ok"], t["message"][:20])
t2 = c.post("/api/settings/test-parse", json={"engine": "builtin"}).json()
print("test-parse:", t2["ok"], t2["message"][:20])

# 8. 前端静态页
r = c.get("/")
print("SPA /:", r.status_code, "len:", len(r.text))
r = c.get("/reader/xyz")
print("SPA fallback /reader/xyz:", r.status_code, "is html:", r.text.lstrip().startswith("<!DOCTYPE"))
print("\nALL TESTS DONE")
