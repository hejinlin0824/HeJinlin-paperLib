# -*- coding: utf-8 -*-
"""清洗 MinerU 公式块中残留的 $$ 包裹（存量数据）。"""
import json
import re
import sqlite3

c = sqlite3.connect('data/library.db')
rows = c.execute(
    "SELECT id, en, meta FROM blocks WHERE type='formula' AND (en LIKE '$$%' OR meta LIKE '%$%latex%$$%')"
).fetchall()
n = 0
for bid, en, meta in rows:
    latex = re.sub(r"^\$\$+|\$\$+$", "", (en or '').strip()).strip()
    if not latex:
        continue
    m = json.loads(meta)
    m['latex'] = latex
    c.execute('UPDATE blocks SET en=?, meta=? WHERE id=?',
              (latex, json.dumps(m, ensure_ascii=False), bid))
    n += 1
c.commit()
print('cleaned formula blocks:', n)
