# -*- coding: utf-8 -*-
"""一次性：全库块文本 HTML 标签清理（<sup>n</sup>→[n]，其余剥壳）。"""
import re
import sqlite3

c = sqlite3.connect('data/library.db')
rows = c.execute(
    "SELECT id, en, zh FROM blocks WHERE en LIKE '%<%' OR zh LIKE '%<%'").fetchall()
n = 0
for bid, en, zh in rows:
    new_en = re.sub(r"<sup>(.*?)</sup>", r"[\1]", en or '', flags=re.I | re.S)
    new_en = re.sub(r"</?[a-zA-Z][^>]*>", "", new_en)
    new_zh = re.sub(r"<sup>(.*?)</sup>", r"[\1]", zh or '', flags=re.I | re.S)
    new_zh = re.sub(r"</?[a-zA-Z][^>]*>", "", new_zh)
    if new_en != en or new_zh != zh:
        c.execute('UPDATE blocks SET en=?, zh=? WHERE id=?', (new_en, new_zh, bid))
        n += 1
c.commit()
print('HTML 清理块数:', n)
