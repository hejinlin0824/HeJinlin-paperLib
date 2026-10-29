"""全局路径与常量配置。"""
from pathlib import Path

# 项目根目录 = backend/app/config.py 上三级
BASE_DIR = Path(__file__).resolve().parent.parent.parent

DATA_DIR = BASE_DIR / "data"
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH = DATA_DIR / "library.db"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PAPERS_DIR.mkdir(parents=True, exist_ok=True)

HOST = "127.0.0.1"
PORT = 8300

# 可翻译的块类型（公式/图片二进制等结构块跳过；image 的 en 是图注文本，可翻）
TRANSLATABLE_TYPES = {"text", "heading", "image", "table", "list"}

# 研究总表状态
LOG_STATUS = ["在读", "读完", "搁置"]
