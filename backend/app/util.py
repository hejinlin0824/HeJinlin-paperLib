"""杂项工具。"""
import uuid
from datetime import datetime


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def loads(s, default=None):
    if not s:
        return default if default is not None else {}
    try:
        import json
        return json.loads(s)
    except Exception:
        return default if default is not None else {}


def dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
